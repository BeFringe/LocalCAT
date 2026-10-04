"""Core-issued performance warnings through Host, Controller and cold restore."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capability_host as host
import qt_editor
from editor_contracts import EditorProject, EditorSegment, FuzzyValidationState
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_benchmark_gate import combine_benchmark_evidence
from tm_contracts import BenchmarkExecutionPath, CandidateStage, TMQuery, TMResourceHandle
from tm_retrieval_capability import RetrievalCapabilityEvaluator
from tests.source_authority_support import current_source_authority
from tests.test_capability_host_gate_d import (
    _EVALUATED_AT, _MetadataQueryView, _MetadataStore, _ZeroCandidateRetriever,
    _gate_c, _gate_d_binding, _private_service,
)
from tests import test_tm_benchmark_gate as benchmark_tests
from tests import test_tm_retrieval_capability as core_tests


def _warning_bundle(*, rss_blocked=False):
    fts5, fts5_oracle = benchmark_tests._fts5_fixture(
        missing_top10_queries=0, latency_scale=100_000,
        peak_rss_bytes=600_000_000 if rss_blocked else 300_000_000,
    )
    fallback, fallback_oracle = benchmark_tests._fallback_fixture(
        missing_top10_queries=0, migration_elapsed_ns=121_000_000_000,
    )
    return combine_benchmark_evidence(fts5, fallback, fts5_oracle, fallback_oracle)


def _compose_warning_host(*, state_root=None):
    composition = host.compose_capability_host(
        source_authority=current_source_authority(), evaluated_at_utc=_EVALUATED_AT,
        gate_d_attestation_root=state_root,
    )
    _gate_c(composition)
    return composition


def _publish_warning_bundle(composition, bundle):
    """Inject only measured runner output; retain Core validation/publication."""
    binding = _gate_d_binding(composition)

    def execute(**kwargs):
        return host._CoreGateDPublication(
            _mint=host._GATE_D_PUBLICATION_MINT, binding=binding,
            run_result=benchmark_tests.GateDPublicationTests._run_result(bundle),
            publication_owner_identity=kwargs["publication_owner_identity"],
            publication_graph_nonce=kwargs["publication_graph_nonce"],
        )

    owner = composition.retrieval_gate_d_owner
    with patch.object(owner, "_RetrievalGateDOwner__execute", side_effect=execute):
        owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        return owner.wait(timeout=10)


def _warning_controller(root, composition):
    repository = ResourceRepository(root / "app-data")
    adapter = EditorTMAdapter(
        runtime_host=TMRuntimeHost(resolver=TMResourceResolver(), configs=repository.list_resources()),
        capability_host=composition.host,
        fuzzy_validation_status=lambda: qt_editor._fuzzy_validation_display(composition),
        fuzzy_validation_poll=lambda: qt_editor._poll_fuzzy_validation_display(composition),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(EditorProject(name="Warnings", segments=(EditorSegment(id="one", source="Hello"),)))
    return controller


class CapabilityHostPerformanceWarningTests(unittest.TestCase):
    def test_only_effectively_open_paths_contribute_in_core_order(self):
        evidence = core_tests.TimePerformanceAdmissionTests.evidence
        for fts5_failures, fallback_failures, expected in (
            (("MIGRATION",), ("EXACT_P95", "FUZZY_P95"), ("EXACT_P95", "FUZZY_P95", "MIGRATION")),
            (("EXACT_P95", "PEAK_RSS"), ("MIGRATION",), ("MIGRATION",)),
        ):
            with self.subTest(fts5=fts5_failures):
                snapshot = RetrievalCapabilityEvaluator(core_tests._expectation()).evaluate(
                    core_tests._manifest(
                        fts5_benchmark=evidence(BenchmarkExecutionPath.FTS5_TRIGRAM, fts5_failures),
                        gram_benchmark=evidence(BenchmarkExecutionPath.GRAM_FALLBACK, fallback_failures),
                    ), evaluated_at_utc=core_tests.EVALUATED_AT,
                )
                display = host._retrieval_display(snapshot)
                self.assertTrue(display.fuzzy_available)
                self.assertEqual(display.performance_warning_codes, expected)
                self.assertFalse(set(expected) & set(display.safe_codes))
                closed_code = "TM.RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_FAILED"
                closed = replace(snapshot, fuzzy_core=replace(snapshot.fuzzy_core, available=False, unavailable_code=closed_code),
                                 summary=replace(snapshot.summary, unavailable_codes=tuple(sorted({*snapshot.summary.unavailable_codes, closed_code}))))
                closed_display = host._retrieval_display(closed)
                self.assertFalse(closed_display.fuzzy_available)
                self.assertEqual(closed_display.performance_warning_codes, ())

    def test_real_publication_and_controller_clones_preserve_warnings(self):
        with tempfile.TemporaryDirectory() as temporary:
            composition = _compose_warning_host()
            controller = _warning_controller(Path(temporary), composition)
            before = controller.poll_tm_threshold_display()
            self.assertFalse(before.retrieval.fuzzy_available)
            bundle = _warning_bundle(rss_blocked=True)
            completed = _publish_warning_bundle(composition, bundle)
            self.assertIs(completed.state, host.GateDRunState.SUCCEEDED)
            self.assertFalse(bundle.suite_report.passed)
            capability = _private_service(composition.host.retrieval_snapshot())._capability_publisher.snapshot()
            self.assertFalse(capability.fuzzy_available_for("FTS5_TRIGRAM")[0])
            self.assertTrue(capability.fuzzy_available_for("GRAM_FALLBACK")[0])
            self._assert_mixed_query_keeps_rss_resource_closed(composition)
            for display in (composition.host.retrieval_operation_snapshot().display,
                            composition.host.status_snapshot().retrieval,
                            controller.tm_retrieval_status(),
                            controller.tm_suggestion_report().retrieval_status,
                            controller.poll_tm_threshold_display().retrieval):
                self.assertTrue(display.fuzzy_available)
                self.assertEqual(display.performance_warning_codes, ("MIGRATION",))
                object.__setattr__(display, "performance_warning_codes", ())
            fresh = controller.poll_tm_threshold_display()
            self.assertIs(fresh.validation.state, FuzzyValidationState.SUCCEEDED)
            self.assertEqual(fresh.retrieval.performance_warning_codes, ("MIGRATION",))
            controller._tm_runtime_blocked_safe_code = "TM.RUNTIME.UNAVAILABLE"
            blocked = controller.poll_tm_threshold_display()
            self.assertFalse(blocked.retrieval.fuzzy_available)
            self.assertEqual(blocked.retrieval.performance_warning_codes, ())

    def _assert_mixed_query_keeps_rss_resource_closed(self, composition):
        class FallbackView(_MetadataQueryView):
            resource_id = "tm.fallback"

            def health(self):
                return replace(super().health(), index_kind="GRAM_FALLBACK")

        class FallbackStore(_MetadataStore):
            @contextmanager
            def query_lease(self):
                yield FallbackView()

        class FallbackRetriever(_ZeroCandidateRetriever):
            def candidates_from_view(self, resource_id, view, folded_query, *, result_limit):
                if resource_id != "tm.fallback":
                    raise AssertionError("RSS-rejected path must not retrieve candidates")
                result = super().candidates_from_view(resource_id, view, folded_query, result_limit=result_limit)
                return replace(result, metadata=replace(
                    result.metadata, index_kind="GRAM_FALLBACK",
                    stages=tuple(replace(stage, stage=CandidateStage.GRAM_3)
                                 if stage.stage is CandidateStage.FTS_TRIGRAM else stage
                                 for stage in result.metadata.stages),
                ))

        service = _private_service(composition.host.retrieval_snapshot())
        with patch.object(service, "_retriever", FallbackRetriever()):
            report = composition.host.retrieval_snapshot().query_port.query(
                (TMResourceHandle("tm.race", _MetadataStore(), True, True, False, 0),
                 TMResourceHandle("tm.fallback", FallbackStore(), True, True, False, 1)),
                TMQuery("source", None, None, None, 0.60, 10, ("tm.race", "tm.fallback")),
            )
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(len(report.resource_metadata), 2)
        states = {item.resource_id: item for item in report.resource_metadata}
        self.assertFalse(states["tm.race"].recall.fuzzy_available)
        self.assertTrue(states["tm.fallback"].recall.fuzzy_available)

    def test_real_attestation_restore_keeps_warning_without_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "qualification"
            first = _compose_warning_host(state_root=state_root)
            self.assertIs(_publish_warning_bundle(first, _warning_bundle()).state, host.GateDRunState.SUCCEEDED)
            second = _compose_warning_host(state_root=state_root)
            owner = second.retrieval_gate_d_owner
            with patch.object(owner, "_RetrievalGateDOwner__execute", side_effect=AssertionError("restore must not run benchmark")):
                restored = owner.restore_gate_d(evaluated_at_utc=_EVALUATED_AT)
            self.assertIs(restored.state, host.GateDRunState.SUCCEEDED)
            display = _warning_controller(root, second).poll_tm_threshold_display()
            self.assertTrue(display.retrieval.fuzzy_available)
            self.assertEqual(display.retrieval.performance_warning_codes, ("EXACT_P95", "FUZZY_P95", "MIGRATION"))

    def test_failed_or_cancelled_run_never_publishes_warning(self):
        for code in ("GATE_D.BENCHMARK_FAILED", "GATE_D.CANCELLED"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                composition = _compose_warning_host()
                owner = composition.retrieval_gate_d_owner
                controller = _warning_controller(Path(temporary), composition)
                with patch.object(owner, "_RetrievalGateDOwner__execute", side_effect=host._GateDOperationalError(code)):
                    owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                    owner.wait(timeout=10)
                display = controller.poll_tm_threshold_display()
                self.assertIs(display.validation.state, FuzzyValidationState.FAILED)
                self.assertEqual(display.validation.safe_code, code)
                self.assertTrue(display.retrieval.context_available)
                self.assertFalse(display.retrieval.fuzzy_available)
                self.assertEqual(display.retrieval.performance_warning_codes, ())
