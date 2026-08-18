"""Task 4.1 canonical current-segment adapter tests."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from threading import Event, Thread
from typing import Any, cast
import unittest
from unittest.mock import patch

from capability_host import CapabilityHostComposition
from editor_contracts import (
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMPreferences,
)
from editor_tm_adapter import EditorTMAdapter
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    QueryReport,
    ResourceQueryFailure,
    TMMatchType,
    TMQuery,
    TMResourceHandle,
)
from tm_migration import TMMigrationService
from tm_retrieval import TMRetrievalService
from tm_sqlite_store import ResourceStoreCoordinator
from tm_retrieval_capability import RetrievalCapabilityPublisher
from tests.test_capability_host_gate_d import (
    _FakeGateDExecution,
    _composition as _gate_d_composition,
    _gate_c,
    _gate_d_owner,
)


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _activate(
    root: Path,
    *,
    resource_id: str,
    rows: tuple[str, ...],
) -> Path:
    source = (root / f"{resource_id}.jsonl").resolve()
    source.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        resource_id,
        source,
    )
    canonical_store_id = f"store.{resource_id}"
    coordinator = ResourceStoreCoordinator(
        canonical_store_id=canonical_store_id,
        resource_identity=identity,
    )
    outcome = TMMigrationService(
        resource_identity=identity,
        canonical_store_id=canonical_store_id,
        coordinator=coordinator,
    ).activate_initial(source, resource_id)
    if type(outcome) is not MigrationReport:
        raise AssertionError(f"canonical fixture activation failed: {outcome!r}")
    return source


def _config(
    *,
    resource_id: str,
    path: Path,
    active: bool,
    lookup: bool,
) -> ResourceConfig:
    return ResourceConfig(
        id=resource_id,
        name=resource_id,
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=path,
        active=active,
        lookup=lookup,
        update=False,
    )


def _private_retrieval_service(
    composition: CapabilityHostComposition,
) -> TMRetrievalService:
    handoff = composition.host.retrieval_snapshot()
    service_field = dataclasses.fields(cast(Any, handoff.query_port))[0].name
    service = getattr(handoff.query_port, service_field)
    if type(service) is not TMRetrievalService:
        raise AssertionError("expected production retrieval service")
    return service


class EditorTMAdapterCanonicalTests(unittest.TestCase):
    def _open_capability_host(self) -> CapabilityHostComposition:
        execution = _FakeGateDExecution()
        composition = _gate_d_composition(self, execution)
        _ = _gate_c(composition)
        owner = _gate_d_owner(composition)
        _ = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
        status = owner.wait(timeout=10.0)
        self.assertEqual(status.state.value, "SUCCEEDED")
        self.assertTrue(
            composition.host.retrieval_snapshot().display.context_available
        )
        self.assertTrue(
            composition.host.retrieval_snapshot().display.fuzzy_available
        )
        return composition

    def _adapter_fixture(
        self,
        root: Path,
    ) -> tuple[
        EditorTMAdapter,
        TMRuntimeHost,
        tuple[ResourceConfig, ...],
        CapabilityHostComposition,
    ]:
        dormant = _activate(
            root,
            resource_id="tm.dormant",
            rows=('{"source":"aabba","target":"dormant"}',),
        )
        primary = _activate(
            root,
            resource_id="tm.primary",
            rows=(
                '{"source":"aabba","target":"context",'
                '"speaker":"Narrator"}',
                '{"source":"aabba","target":"exact",'
                '"speaker":"Other"}',
                '{"source":"bbaab","target":"boundary"}',
                '{"source":"bbbaa","target":"below"}',
                '{"source":"AABBA","target":"one-hundred"}',
            ),
        )
        no_lookup = _activate(
            root,
            resource_id="tm.no-lookup",
            rows=('{"source":"aabba","target":"hidden"}',),
        )
        configs = (
            _config(
                resource_id="tm.dormant",
                path=dormant,
                active=False,
                lookup=True,
            ),
            _config(
                resource_id="tm.primary",
                path=primary,
                active=True,
                lookup=True,
            ),
            _config(
                resource_id="tm.no-lookup",
                path=no_lookup,
                active=True,
                lookup=False,
            ),
        )
        runtime_host = TMRuntimeHost(
            resolver=TMResourceResolver(),
            configs=configs,
        )
        capability = self._open_capability_host()
        return (
            EditorTMAdapter(
                runtime_host=runtime_host,
                capability_host=capability.host,
            ),
            runtime_host,
            configs,
            capability,
        )

    def test_maps_raw_current_segment_and_preserves_core_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, runtime_host, _configs, _capability = self._adapter_fixture(
                Path(temporary)
            )
            runtime = runtime_host.snapshot()
            self.assertEqual(
                tuple(handle.order for handle in runtime.canonical_handles),
                (0, 1, 2),
            )

            batch = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(
                    id="segment-7",
                    source="aabba",
                    speaker="Narrator",
                ),
                project_session_id="project-session-1",
                query_epoch=7,
                preferences=TMPreferences(minimum_similarity=0.60),
            )

            self.assertEqual(batch.query.query_source, "aabba")
            self.assertEqual(batch.query.speaker_raw, "Narrator")
            self.assertIsNone(batch.query.context_prev_raw)
            self.assertIsNone(batch.query.context_next_raw)
            self.assertEqual(batch.query.minimum_similarity, 0.60)
            self.assertEqual(batch.query.limit, 10)
            self.assertEqual(batch.query.resource_order, ("tm.primary",))
            self.assertIsNot(batch.runtime, runtime)
            self.assertEqual(batch.runtime.generation, runtime.generation)
            self.assertTrue(batch.retrieval.display.fuzzy_available)
            self.assertEqual(batch.report.resource_failures, ())
            self.assertEqual(
                tuple(
                    (
                        result.resource_id,
                        result.record_id,
                        result.target,
                        result.match_type,
                        result.similarity,
                        result.matched_source,
                        result.stable_tie_key,
                    )
                    for result in batch.report.results
                ),
                (
                    (
                        "tm.primary",
                        2,
                        "exact",
                        TMMatchType.EXACT,
                        1.0,
                        "aabba",
                        (0, 2),
                    ),
                    (
                        "tm.primary",
                        1,
                        "context",
                        TMMatchType.CONTEXT,
                        1.0,
                        "aabba",
                        (0, 1),
                    ),
                    (
                        "tm.primary",
                        5,
                        "one-hundred",
                        TMMatchType.FUZZY,
                        1.0,
                        "AABBA",
                        (0, 5),
                    ),
                    (
                        "tm.primary",
                        3,
                        "boundary",
                        TMMatchType.FUZZY,
                        0.60,
                        "bbaab",
                        (0, 3),
                    ),
                ),
            )
            self.assertNotIn(
                "below",
                tuple(result.target for result in batch.report.results),
            )
            self.assertNotIn(
                "dormant",
                tuple(result.target for result in batch.report.results),
            )
            self.assertNotIn(
                "hidden",
                tuple(result.target for result in batch.report.results),
            )

    def test_each_threshold_builds_a_fresh_query_and_core_applies_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, _capability = self._adapter_fixture(
                Path(temporary)
            )
            segment = EditorSegment(
                id="segment-7",
                source="aabba",
                speaker="Narrator",
            )
            at_boundary = cast(Any, adapter)._query_canonical(
                segment=segment,
                project_session_id="project-session-1",
                query_epoch=7,
                preferences=TMPreferences(minimum_similarity=0.60),
            )
            above_boundary = cast(Any, adapter)._query_canonical(
                segment=segment,
                project_session_id="project-session-1",
                query_epoch=8,
                preferences=TMPreferences(minimum_similarity=0.61),
            )
            at_one = cast(Any, adapter)._query_canonical(
                segment=segment,
                project_session_id="project-session-1",
                query_epoch=9,
                preferences=TMPreferences(minimum_similarity=1.00),
            )

            self.assertIsNot(at_boundary.query, above_boundary.query)
            self.assertIsNot(above_boundary.query, at_one.query)
            self.assertIn(
                ("boundary", 0.60),
                tuple(
                    (result.target, result.similarity)
                    for result in at_boundary.report.results
                ),
            )
            self.assertNotIn(
                "boundary",
                tuple(result.target for result in above_boundary.report.results),
            )
            self.assertEqual(
                tuple(
                    (result.target, result.match_type, result.similarity)
                    for result in at_one.report.results
                ),
                (
                    ("exact", TMMatchType.EXACT, 1.0),
                    ("context", TMMatchType.CONTEXT, 1.0),
                    ("one-hundred", TMMatchType.FUZZY, 1.0),
                ),
            )

    def test_empty_canonical_cohort_calls_core_once_for_capability_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            termbase_path = (root / "terms.csv").resolve()
            termbase_path.write_text("source,target\n", encoding="utf-8")
            runtime_host = TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=(
                    ResourceConfig(
                        id="terms.only",
                        name="Terms only",
                        kind=ResourceKind.TERMBASE,
                        path=termbase_path,
                    ),
                ),
            )
            capability = self._open_capability_host()
            adapter = EditorTMAdapter(
                runtime_host=runtime_host,
                capability_host=capability.host,
            )
            original_snapshot = RetrievalCapabilityPublisher.snapshot
            captures: list[object] = []
            service = _private_retrieval_service(capability)
            original_query = service._query_reserved
            queries: list[tuple[TMResourceHandle, ...]] = []

            def count_snapshot(publisher: RetrievalCapabilityPublisher) -> object:
                captured = original_snapshot(publisher)
                captures.append(captured)
                return captured

            def count_query(
                resources: tuple[TMResourceHandle, ...],
                query: TMQuery,
                reservation: object,
            ) -> object:
                queries.append(resources)
                return original_query(
                    resources,
                    query,
                    cast(Any, reservation),
                )

            with (
                patch.object(
                    RetrievalCapabilityPublisher,
                    "snapshot",
                    count_snapshot,
                ),
                patch.object(service, "_query_reserved", count_query),
            ):
                batch = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(id="segment-1", source="source"),
                    project_session_id="project-session-1",
                    query_epoch=0,
                    preferences=TMPreferences(),
                )

            self.assertEqual(queries, [()])
            self.assertEqual(len(captures), 1)
            self.assertEqual(batch.query.resource_order, ())
            self.assertEqual(batch.report.results, ())
            self.assertEqual(batch.report.resource_failures, ())
            self.assertEqual(batch.report.resource_metadata, ())

    def test_inflight_query_retains_one_old_runtime_and_retrieval_pair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, runtime_host, configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            old_runtime_public = runtime_host.snapshot()
            old_retrieval_public = capability.host.retrieval_snapshot()
            service = _private_retrieval_service(capability)
            original_query = service._query_reserved
            entered = Event()
            release = Event()
            outcome: list[object] = []
            failures: list[BaseException] = []
            refreshed_retrieval: list[object] = []
            refresh_failures: list[BaseException] = []

            def blocking_query(
                resources: tuple[TMResourceHandle, ...],
                query: TMQuery,
                reservation: object,
            ) -> object:
                entered.set()
                if not release.wait(timeout=5.0):
                    raise AssertionError("query release timed out")
                return original_query(
                    resources,
                    query,
                    cast(Any, reservation),
                )

            def run_query() -> None:
                try:
                    outcome.append(
                        cast(Any, adapter)._query_canonical(
                            segment=EditorSegment(
                                id="segment-7",
                                source="aabba",
                                speaker="Narrator",
                            ),
                            project_session_id="project-session-1",
                            query_epoch=7,
                            preferences=TMPreferences(minimum_similarity=0.60),
                        )
                    )
                except BaseException as error:
                    failures.append(error)

            def refresh_capability() -> None:
                try:
                    refreshed_retrieval.append(_gate_c(capability))
                except BaseException as error:
                    refresh_failures.append(error)

            with patch.object(
                service,
                "_query_reserved",
                blocking_query,
            ):
                worker = Thread(target=run_query, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(timeout=5.0))
                refreshed_runtime = runtime_host.refresh(configs)
                refresher = Thread(target=refresh_capability, daemon=True)
                refresher.start()
                release.set()
                worker.join(timeout=10.0)
                refresher.join(timeout=10.0)

            self.assertFalse(worker.is_alive())
            self.assertFalse(refresher.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(refresh_failures, [])
            self.assertEqual(len(outcome), 1)
            self.assertEqual(len(refreshed_retrieval), 1)
            batch = cast(Any, outcome[0])
            self.assertIsNot(batch.runtime, old_runtime_public)
            self.assertIsNot(batch.runtime, refreshed_runtime)
            self.assertIs(
                batch.runtime.canonical_handles[1].store,
                old_runtime_public.canonical_handles[1].store,
            )
            self.assertIsNot(
                batch.runtime.canonical_handles[1].store,
                refreshed_runtime.canonical_handles[1].store,
            )
            self.assertIsNot(batch.retrieval, old_retrieval_public)
            self.assertIsNot(batch.retrieval, refreshed_retrieval[0])
            self.assertTrue(batch.retrieval.display.fuzzy_available)
            self.assertFalse(
                cast(Any, refreshed_retrieval[0]).display.fuzzy_available
            )

    def test_rejects_real_core_report_from_outside_captured_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            runtime = runtime_host.snapshot()
            dormant = runtime.canonical_handles[0]
            wrong_handle = TMResourceHandle(
                resource_id=dormant.resource_id,
                store=dormant.store,
                active=True,
                lookup=True,
                update=dormant.update,
                order=0,
            )
            wrong_query = TMQuery(
                query_source="aabba",
                speaker_raw="Narrator",
                context_prev_raw=None,
                context_next_raw=None,
                minimum_similarity=0.60,
                limit=10,
                resource_order=(wrong_handle.resource_id,),
            )
            wrong_report = capability.host.retrieval_snapshot().query_port.query(
                (wrong_handle,),
                wrong_query,
            )
            self.assertEqual(
                tuple(result.resource_id for result in wrong_report.results),
                ("tm.dormant",),
            )
            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=wrong_report,
                ),
                self.assertRaisesRegex(ValueError, "outside canonical cohort"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=7,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

    def test_gate_d_publication_waits_for_query_capability_capture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _unused, runtime_host, _configs, _opened = self._adapter_fixture(
                Path(temporary)
            )
            execution = _FakeGateDExecution()
            capability = _gate_d_composition(self, execution)
            gate_c_handoff = _gate_c(capability)
            self.assertFalse(gate_c_handoff.display.fuzzy_available)
            adapter = EditorTMAdapter(
                runtime_host=runtime_host,
                capability_host=capability.host,
            )
            service = _private_retrieval_service(capability)
            original_query = service._query_reserved
            entered = Event()
            release = Event()
            outcomes: list[object] = []
            failures: list[BaseException] = []

            def blocking_query(
                resources: tuple[TMResourceHandle, ...],
                query: TMQuery,
                reservation: object,
            ) -> object:
                entered.set()
                if not release.wait(timeout=5.0):
                    raise AssertionError("query release timed out")
                return original_query(
                    resources,
                    query,
                    cast(Any, reservation),
                )

            def run_query() -> None:
                try:
                    outcomes.append(
                        cast(Any, adapter)._query_canonical(
                            segment=EditorSegment(
                                id="segment-7",
                                source="aabba",
                                speaker="Narrator",
                            ),
                            project_session_id="project-session-1",
                            query_epoch=7,
                            preferences=TMPreferences(minimum_similarity=0.60),
                        )
                    )
                except BaseException as error:
                    failures.append(error)

            owner = _gate_d_owner(capability)
            with patch.object(
                service,
                "_query_reserved",
                blocking_query,
            ):
                worker = Thread(target=run_query, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(timeout=5.0))
                _ = owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                self.assertTrue(execution.started.wait(timeout=5.0))
                published_while_query_blocked = (
                    execution.publication_started.wait(timeout=0.2)
                )
                observed_handoff = capability.host.retrieval_snapshot()
                release.set()
                worker.join(timeout=10.0)
                finished = owner.wait(timeout=10.0)

            self.assertFalse(worker.is_alive())
            self.assertFalse(published_while_query_blocked)
            self.assertIs(observed_handoff, gate_c_handoff)
            self.assertEqual(finished.state.value, "SUCCEEDED")
            self.assertEqual(failures, [])
            self.assertEqual(len(outcomes), 1)
            batch = cast(Any, outcomes[0])
            self.assertFalse(batch.retrieval.display.fuzzy_available)
            self.assertFalse(
                any(
                    result.match_type is TMMatchType.FUZZY
                    for result in batch.report.results
                )
            )

    def test_revalidates_full_nested_result_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            valid = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(
                    id="segment-7",
                    source="aabba",
                    speaker="Narrator",
                ),
                project_session_id="project-session-1",
                query_epoch=7,
                preferences=TMPreferences(minimum_similarity=0.60),
            )
            exact = valid.report.results[0]
            self.assertIs(exact.match_type, TMMatchType.EXACT)
            object.__setattr__(exact, "similarity", 0.25)
            valid.report.__post_init__()

            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=valid.report,
                ),
                self.assertRaisesRegex(ValueError, "similarity must be 1.0"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=8,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

    def test_rejects_publisher_drift_before_query_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            handoff = capability.host.retrieval_snapshot()
            self.assertTrue(handoff.display.fuzzy_available)
            service_field = dataclasses.fields(cast(Any, handoff.query_port))[0].name
            service = getattr(handoff.query_port, service_field)
            publisher = cast(Any, service)._capability_publisher
            closed = publisher.refresh(None, evaluated_at_utc=_EVALUATED_AT)
            self.assertFalse(closed.fuzzy_core.available)
            calls = 0

            def forbidden_query(
                resources: tuple[TMResourceHandle, ...],
                query: TMQuery,
                reservation: object,
            ) -> QueryReport:
                nonlocal calls
                del resources, query, reservation
                calls += 1
                return QueryReport((), (), ())

            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    forbidden_query,
                ),
                self.assertRaisesRegex(ValueError, "retrieval capability drift"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=8,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

            self.assertEqual(calls, 0)

    def test_revalidates_nested_failure_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            failure = ResourceQueryFailure(
                resource_id="tm.primary",
                stage="QUERY",
                error_code="TM.QUERY.UNAVAILABLE",
                retryable=False,
            )
            object.__setattr__(failure, "retryable", 1)
            forged_failure_report = QueryReport((), (failure,), ())
            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=forged_failure_report,
                ),
                self.assertRaisesRegex(TypeError, "failure retryable"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=8,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

    def test_revalidates_nested_recall_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            valid = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(
                    id="segment-7",
                    source="aabba",
                    speaker="Narrator",
                ),
                project_session_id="project-session-1",
                query_epoch=7,
                preferences=TMPreferences(minimum_similarity=0.60),
            )
            recall = valid.report.resource_metadata[0].recall
            object.__setattr__(recall, "index_kind", "FORGED")
            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=valid.report,
                ),
                self.assertRaisesRegex(ValueError, "index kind is unsupported"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=9,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

    def test_requires_complete_cohort_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            first = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(
                    id="segment-7",
                    source="aabba",
                    speaker="Narrator",
                ),
                project_session_id="project-session-1",
                query_epoch=7,
                preferences=TMPreferences(minimum_similarity=0.60),
            )
            object.__setattr__(first.report, "results", ())
            object.__setattr__(first.report, "resource_metadata", ())
            first.report.__post_init__()
            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=first.report,
                ),
                self.assertRaisesRegex(ValueError, "cover canonical cohort"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=8,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )

    def test_rejects_reversed_core_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter, _runtime_host, _configs, capability = self._adapter_fixture(
                Path(temporary)
            )
            second = cast(Any, adapter)._query_canonical(
                segment=EditorSegment(
                    id="segment-7",
                    source="aabba",
                    speaker="Narrator",
                ),
                project_session_id="project-session-1",
                query_epoch=9,
                preferences=TMPreferences(minimum_similarity=0.60),
            )
            object.__setattr__(
                second.report,
                "results",
                tuple(reversed(second.report.results)),
            )
            second.report.__post_init__()
            with (
                patch.object(
                    _private_retrieval_service(capability),
                    "_query_reserved",
                    return_value=second.report,
                ),
                self.assertRaisesRegex(ValueError, "Core stable order"),
            ):
                _ = cast(Any, adapter)._query_canonical(
                    segment=EditorSegment(
                        id="segment-7",
                        source="aabba",
                        speaker="Narrator",
                    ),
                    project_session_id="project-session-1",
                    query_epoch=10,
                    preferences=TMPreferences(minimum_similarity=0.60),
                )


if __name__ == "__main__":
    unittest.main()
