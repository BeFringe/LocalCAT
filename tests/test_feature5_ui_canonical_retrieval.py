"""Task 7.3 canonical/mixed retrieval integration acceptance.

The tests deliberately build their canonical resources through the Task 7.2
production activation fixture and execute the real CapabilityHost retrieval
port.  Assertions compare the adapter projection with the Core report from
the same operation; no test-side scorer, UI filter, or ordering substitute is
used as acceptance evidence.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    EditorSegment,
    ResourceConfig,
    ResourceKind,
    TMPreferences,
    TMSuggestionReport,
)
from editor_tm_adapter import EditorTMAdapter
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import QueryReport, TMMatchType, TMQuery, TMResourceHandle
from tests.feature5_ui_canonical_fixture import (
    BELOW_THRESHOLD_SOURCE,
    BOUNDARY_SOURCE,
    HIGH_FUZZY_SOURCE,
    ONE_HUNDRED_FUZZY_SOURCE,
    QUERY_SOURCE,
    ActivatedCanonicalFixture,
    build_activated_canonical_fixture,
)
from tests.test_editor_tm_adapter_mixed import (
    _open_capability as _open_validated_capability,
)


_SPEAKER = "Narrator"


def _canonical_configs(
    fixture: ActivatedCanonicalFixture,
) -> tuple[ResourceConfig, ...]:
    return tuple(
        ResourceConfig(
            id=resource.resource_id,
            name=resource.resource_id,
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=resource.identity.configured_jsonl_path,
            active=True,
            lookup=True,
            update=False,
        )
        for resource in fixture.resources
    )


def _legacy_config(root: Path, position: int) -> ResourceConfig:
    resource_id = f"tm.legacy.{position}"
    source = (root / f"legacy-{position}.jsonl").resolve()
    source.write_text(
        '{"source":"aabba","target":"legacy-%d"}\n' % position,
        encoding="utf-8",
    )
    return ResourceConfig(
        id=resource_id,
        name=resource_id,
        kind=ResourceKind.TRANSLATION_MEMORY,
        path=source,
        active=True,
        lookup=True,
        update=False,
    )


def _adapter(
    test_case: unittest.TestCase,
    configs: tuple[ResourceConfig, ...],
) -> tuple[EditorTMAdapter, CapabilityHost]:
    capability = _open_validated_capability(test_case)
    host = capability.host
    return (
        EditorTMAdapter(
            runtime_host=TMRuntimeHost(
                resolver=TMResourceResolver(),
                configs=configs,
            ),
            capability_host=host,
        ),
        host,
    )


def _query(
    adapter: EditorTMAdapter,
    *,
    minimum_similarity: float,
) -> TMSuggestionReport:
    return adapter.query_current(
        segment=EditorSegment(
            id="segment-7.3",
            source=QUERY_SOURCE,
            speaker=_SPEAKER,
        ),
        project_session_id="task-7.3-session",
        query_epoch=73,
        preferences=TMPreferences(
            minimum_similarity=minimum_similarity,
        ),
    )


def _capturing_query(
    captured: list[tuple[TMQuery, QueryReport]],
) -> tuple[object, Any]:
    original = CapabilityHost.query_retrieval_operation

    def capture(
        host: CapabilityHost,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> Any:
        operation = original(host, resources, query)
        captured.append((query, operation.report))
        return operation

    return CapabilityHost, capture


def _core_projection(report: QueryReport) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            result.resource_id,
            f"canonical:{result.record_id}",
            result.query_source,
            result.matched_source,
            result.target,
            result.match_type,
            result.similarity,
        )
        for result in report.results
    )


def _suggestion_projection(
    report: TMSuggestionReport,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            suggestion.resource_id,
            suggestion.record_id,
            suggestion.query_source,
            suggestion.matched_source,
            suggestion.target,
            suggestion.match_type,
            suggestion.final_similarity,
        )
        for suggestion in report.suggestions
    )


class Feature5UICanonicalRetrievalTests(unittest.TestCase):
    def test_real_core_report_projects_exact_context_fuzzy_and_boundaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            adapter, _host = _adapter(self, _canonical_configs(fixture))
            captured: list[tuple[TMQuery, QueryReport]] = []
            target, capture = _capturing_query(captured)

            with patch.object(
                target,
                "query_retrieval_operation",
                capture,
            ):
                projected = _query(adapter, minimum_similarity=0.60)

            self.assertEqual(len(captured), 1)
            core_query, core_report = captured[0]
            self.assertEqual(core_query.minimum_similarity, 0.60)
            self.assertEqual(len(core_report.results), 10)
            self.assertEqual(
                _suggestion_projection(projected),
                _core_projection(core_report),
            )

            expected = (
                ("tm.fixture.primary", 2, TMMatchType.EXACT, 1.0, QUERY_SOURCE),
                ("tm.fixture.secondary", 2, TMMatchType.EXACT, 1.0, QUERY_SOURCE),
                ("tm.fixture.primary", 1, TMMatchType.CONTEXT, 1.0, QUERY_SOURCE),
                ("tm.fixture.secondary", 1, TMMatchType.CONTEXT, 1.0, QUERY_SOURCE),
                (
                    "tm.fixture.primary",
                    6,
                    TMMatchType.FUZZY,
                    1.0,
                    ONE_HUNDRED_FUZZY_SOURCE,
                ),
                (
                    "tm.fixture.secondary",
                    6,
                    TMMatchType.FUZZY,
                    1.0,
                    ONE_HUNDRED_FUZZY_SOURCE,
                ),
                (
                    "tm.fixture.primary",
                    3,
                    TMMatchType.FUZZY,
                    0.8,
                    HIGH_FUZZY_SOURCE,
                ),
                (
                    "tm.fixture.secondary",
                    3,
                    TMMatchType.FUZZY,
                    0.8,
                    HIGH_FUZZY_SOURCE,
                ),
                (
                    "tm.fixture.primary",
                    4,
                    TMMatchType.FUZZY,
                    0.60,
                    BOUNDARY_SOURCE,
                ),
                (
                    "tm.fixture.secondary",
                    4,
                    TMMatchType.FUZZY,
                    0.60,
                    BOUNDARY_SOURCE,
                ),
            )
            self.assertEqual(
                tuple(
                    (
                        result.resource_id,
                        result.record_id,
                        result.match_type,
                        result.similarity,
                        result.matched_source,
                    )
                    for result in core_report.results
                ),
                expected,
            )
            self.assertNotIn(
                BELOW_THRESHOLD_SOURCE,
                tuple(result.matched_source for result in core_report.results),
            )
            self.assertTrue(
                all(result.query_source == QUERY_SOURCE for result in core_report.results)
            )
            self.assertEqual(
                len(
                    {
                        (result.resource_id, result.record_id)
                        for result in core_report.results
                    }
                ),
                len(core_report.results),
            )
            self.assertEqual(
                tuple(
                    (
                        metadata.recall.union_unique_count,
                        metadata.recall.deduplicated_count,
                        metadata.recall.truncated,
                    )
                    for metadata in core_report.resource_metadata
                ),
                ((6, 6, False), (6, 6, False)),
            )

    def test_same_snapshot_is_deterministic_and_lower_threshold_requeries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            adapter, _host = _adapter(self, _canonical_configs(fixture))
            captured: list[tuple[TMQuery, QueryReport]] = []
            target, capture = _capturing_query(captured)
            generations_before = adapter._current_query_generations()

            with patch.object(
                target,
                "query_retrieval_operation",
                capture,
            ):
                one_hundred = _query(adapter, minimum_similarity=1.0)
                high = _query(adapter, minimum_similarity=0.8)
                boundary = _query(adapter, minimum_similarity=0.60)
                repeated = _query(adapter, minimum_similarity=0.60)

            self.assertEqual(len(captured), 4)
            self.assertEqual(
                tuple(query.minimum_similarity for query, _report in captured),
                (1.0, 0.8, 0.60, 0.60),
            )
            self.assertEqual(
                tuple(len(report.results) for _query_value, report in captured),
                (6, 8, 10, 10),
            )
            self.assertEqual(captured[2][1], captured[3][1])
            self.assertEqual(boundary.suggestions, repeated.suggestions)
            self.assertEqual(
                adapter._current_query_generations(),
                generations_before,
            )
            self.assertIsNot(captured[0][0], captured[1][0])
            self.assertIsNot(captured[1][0], captured[2][0])

            one_hundred_fuzzy = tuple(
                suggestion
                for suggestion in one_hundred.suggestions
                if suggestion.match_type is TMMatchType.FUZZY
            )
            self.assertEqual(len(one_hundred_fuzzy), 2)
            self.assertTrue(
                all(
                    suggestion.final_similarity == 1.0
                    and suggestion.query_source == QUERY_SOURCE
                    and suggestion.matched_source == ONE_HUNDRED_FUZZY_SOURCE
                    for suggestion in one_hundred_fuzzy
                )
            )
            self.assertNotIn(
                BOUNDARY_SOURCE,
                tuple(suggestion.matched_source for suggestion in high.suggestions),
            )
            self.assertEqual(
                tuple(
                    suggestion.matched_source
                    for suggestion in boundary.suggestions
                    if suggestion.matched_source == BOUNDARY_SOURCE
                ),
                (BOUNDARY_SOURCE, BOUNDARY_SOURCE),
            )
            self.assertNotIn(
                BELOW_THRESHOLD_SOURCE,
                tuple(
                    suggestion.matched_source
                    for suggestion in boundary.suggestions
                ),
            )

    def test_interleaved_mixed_resources_apply_one_global_top_ten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_activated_canonical_fixture(root / "canonical")
            canonical = _canonical_configs(fixture)
            configs = (
                _legacy_config(root, 0),
                canonical[0],
                _legacy_config(root, 1),
                canonical[1],
                _legacy_config(root, 2),
            )
            adapter, _host = _adapter(self, configs)
            captured: list[tuple[TMQuery, QueryReport]] = []
            target, capture = _capturing_query(captured)

            with patch.object(
                target,
                "query_retrieval_operation",
                capture,
            ):
                first = _query(adapter, minimum_similarity=0.60)
                repeated = _query(adapter, minimum_similarity=0.60)

            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[0][1], captured[1][1])
            self.assertEqual(first.suggestions, repeated.suggestions)
            self.assertEqual(len(captured[0][1].results), 10)
            self.assertEqual(len(first.suggestions), 10)
            self.assertEqual(len(first.resource_statuses), 5)
            self.assertEqual(
                tuple(status.resource_id for status in first.resource_statuses),
                tuple(config.id for config in configs),
            )

            self.assertEqual(
                tuple(
                    (
                        suggestion.resource_id,
                        suggestion.match_type,
                        suggestion.final_similarity,
                        suggestion.target,
                    )
                    for suggestion in first.suggestions
                ),
                (
                    ("tm.legacy.0", TMMatchType.EXACT, 1.0, "legacy-0"),
                    ("tm.fixture.primary", TMMatchType.EXACT, 1.0, "exact-variant"),
                    ("tm.legacy.1", TMMatchType.EXACT, 1.0, "legacy-1"),
                    ("tm.fixture.secondary", TMMatchType.EXACT, 1.0, "exact-variant"),
                    ("tm.legacy.2", TMMatchType.EXACT, 1.0, "legacy-2"),
                    ("tm.fixture.primary", TMMatchType.CONTEXT, 1.0, "context-variant"),
                    ("tm.fixture.secondary", TMMatchType.CONTEXT, 1.0, "context-variant"),
                    ("tm.fixture.primary", TMMatchType.FUZZY, 1.0, "one-hundred-fuzzy"),
                    ("tm.fixture.secondary", TMMatchType.FUZZY, 1.0, "one-hundred-fuzzy"),
                    ("tm.fixture.primary", TMMatchType.FUZZY, 0.8, "high-fuzzy"),
                ),
            )
            fuzzy = tuple(
                suggestion
                for suggestion in first.suggestions
                if suggestion.match_type is TMMatchType.FUZZY
            )
            self.assertEqual(
                tuple(item.final_similarity for item in fuzzy),
                (1.0, 1.0, 0.8),
            )
            self.assertEqual(
                tuple(item.resource_id for item in fuzzy[:2]),
                ("tm.fixture.primary", "tm.fixture.secondary"),
            )
            self.assertEqual(
                len(
                    {
                        (suggestion.resource_id, suggestion.record_id)
                        for suggestion in first.suggestions
                    }
                ),
                10,
            )

            core_keys = {
                (result.resource_id, f"canonical:{result.record_id}")
                for result in captured[0][1].results
            }
            canonical_suggestions = tuple(
                suggestion
                for suggestion in first.suggestions
                if suggestion.record_id.startswith("canonical:")
            )
            self.assertTrue(
                all(
                    (suggestion.resource_id, suggestion.record_id) in core_keys
                    for suggestion in canonical_suggestions
                )
            )
            self.assertIn(
                BOUNDARY_SOURCE,
                tuple(
                    result.matched_source
                    for result in captured[0][1].results
                ),
            )
            self.assertNotIn(
                BOUNDARY_SOURCE,
                tuple(
                    suggestion.matched_source
                    for suggestion in first.suggestions
                ),
            )


if __name__ == "__main__":
    unittest.main()
