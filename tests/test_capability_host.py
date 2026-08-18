from __future__ import annotations

import dataclasses
import inspect
import unittest
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import patch

from capability_host import (
    CapabilityDisplaySnapshot,
    CapabilityHost,
    MatcherHandoffSnapshot,
    RetrievalHandoffSnapshot,
)
from editor_contracts import RetrievalDisplayState, TextMatcherDisplayState
from tm_contracts import TMQuery, TextMatcherState
from tm_retrieval_capability import (
    RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
    RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCorrectnessCohortEvidence,
    default_retrieval_capability_publisher,
)


_EVALUATED_AT = datetime(2030, 1, 15, tzinfo=timezone.utc)
_GENERATED_UTC = "2030-01-14T00:00:00Z"
_VALID_UNTIL_UTC = "2030-01-16T00:00:00Z"


def _forged_sentinel_manifest(
    publisher: RetrievalCapabilityPublisher,
) -> RetrievalCapabilityManifest:
    expectation = cast(
        Any,
        publisher,
    )._RetrievalCapabilityPublisher__expectation_identity
    return RetrievalCapabilityManifest(
        evidence_schema_version=expectation.evidence_schema_version,
        retrieval_artifact_digest=expectation.retrieval_artifact_digest,
        retrieval_build_digest=expectation.retrieval_build_digest,
        semantics_version=expectation.semantics_version,
        fixture_digest=expectation.fixture_digest,
        evaluator_digest=expectation.evaluator_digest,
        generated_at_utc=_GENERATED_UTC,
        valid_until_utc=_VALID_UNTIL_UTC,
        context_cohorts=tuple(
            RetrievalCorrectnessCohortEvidence(
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort.cohort_digest,
                passed=True,
                generated_at_utc=_GENERATED_UTC,
                valid_until_utc=_VALID_UNTIL_UTC,
            )
            for cohort in expectation.context_cohorts
        ),
        fuzzy_core_cohorts=tuple(
            RetrievalCorrectnessCohortEvidence(
                cohort_id=cohort.cohort_id,
                cohort_digest=cohort.cohort_digest,
                passed=True,
                generated_at_utc=_GENERATED_UTC,
                valid_until_utc=_VALID_UNTIL_UTC,
            )
            for cohort in expectation.fuzzy_core_cohorts
        ),
        fts5_trigram_benchmark=None,
        gram_fallback_benchmark=None,
    )


class ExactOnlyCapabilityHostTests(unittest.TestCase):
    host: CapabilityHost = cast(CapabilityHost, cast(object, None))

    def setUp(self) -> None:
        self.host = CapabilityHost(evaluated_at_utc=_EVALUATED_AT)

    def test_bootstrap_is_closed_and_uses_core_authority_types(self) -> None:
        matcher = self.host.matcher_snapshot()
        retrieval = self.host.retrieval_snapshot()

        self.assertIs(type(matcher), MatcherHandoffSnapshot)
        self.assertEqual(matcher.generation, 0)
        self.assertIsNone(matcher.matcher)
        self.assertIs(type(matcher.display), TextMatcherDisplayState)
        self.assertIs(matcher.display.state, TextMatcherState.UNAVAILABLE)
        self.assertEqual(matcher.display.supported_profiles, ())
        self.assertEqual(
            matcher.display.safe_reason,
            "MATCHER.VALIDATION_UNAVAILABLE",
        )

        self.assertIs(type(retrieval), RetrievalHandoffSnapshot)
        self.assertEqual(retrieval.generation, 0)
        self.assertTrue(callable(retrieval.query_port.query))
        self.assertIs(type(retrieval.display), RetrievalDisplayState)
        self.assertFalse(retrieval.display.context_available)
        self.assertFalse(retrieval.display.fuzzy_available)
        self.assertEqual(
            retrieval.display.safe_codes,
            (
                RETRIEVAL_CONTEXT_EVIDENCE_MISSING_CODE,
                RETRIEVAL_FUZZY_BENCHMARK_EVIDENCE_MISSING_CODE,
                RETRIEVAL_FUZZY_CORRECTNESS_EVIDENCE_MISSING_CODE,
            ),
        )
        self.assertFalse(hasattr(retrieval, "publisher"))
        self.assertFalse(hasattr(retrieval, "service"))
        self.assertEqual(
            tuple(
                name
                for name in dir(retrieval.query_port)
                if not name.startswith("_")
            ),
            ("query",),
        )
        self.assertTrue(dataclasses.is_dataclass(retrieval.query_port))
        self.assertTrue(
            cast(Any, type(retrieval.query_port)).__dataclass_params__.frozen
        )
        self.assertFalse(hasattr(retrieval.query_port, "__dict__"))

    def test_handoff_and_display_snapshots_are_frozen_and_slotted(self) -> None:
        values = (
            self.host.matcher_snapshot(),
            self.host.retrieval_snapshot(),
            self.host.status_snapshot(),
        )
        for value in values:
            with self.subTest(snapshot=type(value).__name__):
                self.assertTrue(dataclasses.is_dataclass(value))
                self.assertTrue(cast(Any, type(value)).__dataclass_params__.frozen)
                self.assertFalse(hasattr(value, "__dict__"))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, dataclasses.fields(value)[0].name, None)

    def test_each_operation_gets_one_stable_snapshot_reference(self) -> None:
        matcher = self.host.matcher_snapshot()
        retrieval = self.host.retrieval_snapshot()
        status = self.host.status_snapshot()

        self.assertIs(self.host.matcher_snapshot(), matcher)
        self.assertIs(self.host.retrieval_snapshot(), retrieval)
        self.assertIs(self.host.status_snapshot(), status)
        self.assertIs(status.matcher, matcher.display)
        self.assertIs(status.retrieval, retrieval.display)

        self.assertIs(self.host.retrieval_snapshot(), retrieval)
        self.assertIs(self.host.status_snapshot(), status)

    def test_retrieval_operation_snapshot_is_defensive_and_host_bound(self) -> None:
        public = self.host.retrieval_snapshot()
        first = self.host.retrieval_operation_snapshot()
        second = self.host.retrieval_operation_snapshot()

        self.assertIsNot(first, public)
        self.assertIsNot(second, first)
        self.assertIsNot(first.query_port, public.query_port)
        self.assertIsNot(first.display, public.display)
        self.assertEqual(first.display, public.display)
        self.assertEqual(first.generation, public.generation)

    def test_retrieval_operation_snapshot_rejects_foreign_host_query_port(
        self,
    ) -> None:
        public = self.host.retrieval_snapshot()
        other = CapabilityHost(evaluated_at_utc=_EVALUATED_AT)
        foreign_port = other.retrieval_snapshot().query_port
        object.__setattr__(public, "query_port", foreign_port)
        public.__post_init__()

        with self.assertRaisesRegex(ValueError, "retrieval handoff drift"):
            self.host.retrieval_operation_snapshot()

    def test_retrieval_operation_snapshot_rejects_display_field_drift(
        self,
    ) -> None:
        public = self.host.retrieval_snapshot()
        object.__setattr__(public.display, "fuzzy_available", True)
        public.display.__post_init__()
        public.__post_init__()

        with self.assertRaisesRegex(ValueError, "retrieval handoff drift"):
            self.host.retrieval_operation_snapshot()

    def test_retrieval_operation_snapshot_rejects_service_publisher_drift(
        self,
    ) -> None:
        public = self.host.retrieval_snapshot()
        service_field = dataclasses.fields(cast(Any, public.query_port))[0].name
        service = getattr(public.query_port, service_field)
        foreign_publisher = default_retrieval_capability_publisher(
            _EVALUATED_AT
        )
        setattr(service, "_capability_publisher", foreign_publisher)

        with self.assertRaisesRegex(ValueError, "retrieval handoff drift"):
            self.host.retrieval_operation_snapshot()

    def test_retrieval_query_uses_core_default_and_captures_once(self) -> None:
        original_snapshot = RetrievalCapabilityPublisher.snapshot
        captured: list[object] = []
        publishers: list[RetrievalCapabilityPublisher] = []

        def capture_default(evaluated_at_utc: datetime) -> RetrievalCapabilityPublisher:
            publisher = default_retrieval_capability_publisher(evaluated_at_utc)
            publishers.append(publisher)
            return publisher

        def count_snapshot(
            publisher: RetrievalCapabilityPublisher,
        ) -> object:
            snapshot = original_snapshot(publisher)
            captured.append(snapshot)
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
        with patch(
            "capability_host.default_retrieval_capability_publisher",
            capture_default,
        ):
            host = CapabilityHost(evaluated_at_utc=_EVALUATED_AT)
        self.assertEqual(len(publishers), 1)
        captured.clear()
        with patch.object(
            RetrievalCapabilityPublisher,
            "snapshot",
            count_snapshot,
        ):
            report = host.retrieval_snapshot().query_port.query((), query)

        self.assertEqual(report.results, ())
        self.assertEqual(report.resource_failures, ())
        self.assertEqual(len(captured), 1)
        self.assertFalse(cast(Any, captured[0]).context.available)
        self.assertFalse(cast(Any, captured[0]).fuzzy_core.available)

    def test_public_handoff_cannot_refresh_or_replace_core_authority(self) -> None:
        retrieval = self.host.retrieval_snapshot()
        public_publisher = getattr(retrieval, "publisher", None)
        promoted = False
        if isinstance(public_publisher, RetrievalCapabilityPublisher):
            forged = _forged_sentinel_manifest(public_publisher)
            refreshed = public_publisher.refresh(
                forged,
                evaluated_at_utc=_EVALUATED_AT,
            )
            promoted = refreshed.context.available or refreshed.fuzzy_core.available

        self.assertFalse(promoted)
        self.assertFalse(hasattr(retrieval, "publisher"))
        self.assertFalse(hasattr(retrieval, "service"))
        self.assertFalse(hasattr(retrieval.query_port, "publisher"))
        self.assertFalse(hasattr(retrieval.query_port, "service"))
        for forbidden in (
            "evaluator",
            "manifest",
            "publish",
            "refresh",
            "replace_publisher",
            "set_capability",
        ):
            self.assertFalse(hasattr(retrieval.query_port, forbidden))
        for name in ("publisher", "service", "refresh"):
            with self.subTest(mutation=name), self.assertRaises(AttributeError):
                setattr(retrieval.query_port, name, object())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cast(Any, retrieval).query_port = object()
        self.assertEqual(retrieval.generation, 0)
        self.assertFalse(retrieval.display.context_available)
        self.assertFalse(retrieval.display.fuzzy_available)

    def test_bootstrap_has_no_boolean_health_or_partial_pass_elevation(self) -> None:
        parameters = inspect.signature(CapabilityHost).parameters
        self.assertEqual(tuple(parameters), ("evaluated_at_utc",))
        with self.assertRaises(TypeError):
            cast(Any, CapabilityHost)(
                evaluated_at_utc=_EVALUATED_AT,
                store_healthy=True,
            )
        with self.assertRaises(TypeError):
            cast(Any, CapabilityHost)(
                evaluated_at_utc=_EVALUATED_AT,
                fuzzy_passed=True,
            )
        for forbidden in (
            "enable_context",
            "enable_fuzzy",
            "publish",
            "refresh",
            "set_available",
            "set_degraded",
            "start_validation",
        ):
            self.assertFalse(hasattr(self.host, forbidden))

    def test_rejects_non_utc_or_implicit_clock_inputs(self) -> None:
        with self.assertRaises(TypeError):
            cast(Any, CapabilityHost)()
        with self.assertRaises((TypeError, ValueError)):
            cast(Any, CapabilityHost)(
                evaluated_at_utc="2030-01-15T00:00:00Z"
            )
        with self.assertRaises(ValueError):
            CapabilityHost(evaluated_at_utc=datetime(2030, 1, 15))


class CapabilityHostContractShapeTests(unittest.TestCase):
    def test_snapshot_field_shapes_are_closed(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(MatcherHandoffSnapshot)),
            ("generation", "matcher", "display"),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(RetrievalHandoffSnapshot)),
            ("generation", "query_port", "display"),
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(CapabilityDisplaySnapshot)),
            ("matcher", "retrieval"),
        )


if __name__ == "__main__":
    unittest.main()
