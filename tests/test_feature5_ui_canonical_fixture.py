"""Task 7.2 real activated-canonical integration fixture acceptance."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import cast
import unittest

from tm_contracts import MigrationReport
from tm_similarity import SimilarityScorerV1
from tm_sqlite_store import (
    SQLiteStoreLifecycleError,
    SQLiteTMQueryView,
    SQLiteTMStore,
)
from tests.feature5_ui_canonical_fixture import (
    BOUNDARY_SOURCE,
    BELOW_THRESHOLD_SOURCE,
    HIGH_FUZZY_SOURCE,
    ONE_HUNDRED_FUZZY_SOURCE,
    QUERY_SOURCE,
    build_activated_canonical_fixture,
)


class Feature5UICanonicalFixtureTests(unittest.TestCase):
    def test_production_activation_cold_reopens_variants_and_query_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = build_activated_canonical_fixture(
                Path(temporary).resolve(),
            )

            self.assertEqual(
                tuple(resource.resource_id for resource in fixture.resources),
                ("tm.fixture.primary", "tm.fixture.secondary"),
            )
            self.assertEqual(fixture.runtime.legacy_ports, ())
            self.assertEqual(len(fixture.runtime.canonical_handles), 2)
            for resource in fixture.resources:
                self.assertIs(type(resource.report), MigrationReport)
                self.assertEqual(resource.report.activated_generation, 0)
                self.assertEqual(resource.report.migrated_count, 6)
                self.assertEqual(resource.report.variant_count, 1)
                self.assertEqual(resource.report.skipped_count, 0)
                self.assertTrue(
                    resource.report.canonical_exact_available
                )
                self.assertFalse(resource.report.context_available)
                self.assertFalse(resource.report.fuzzy_available)
                self.assertTrue(resource.identity.canonical_sidecar_path.is_file())
                self.assertIs(type(resource.handle.store), SQLiteTMStore)

                store = cast(
                    SQLiteTMStore,
                    cast(object, resource.handle.store),
                )
                self.assertEqual(
                    store.coordinator.resource_id,
                    resource.report.resource_id,
                )
                self.assertEqual(
                    store.coordinator.canonical_store_id,
                    resource.report.canonical_store_id,
                )
                self.assertEqual(
                    store.coordinator.current_generation,
                    resource.report.activated_generation,
                )
                with store.query_lease() as view:
                    self.assertIs(type(view), SQLiteTMQueryView)
                    self.assertEqual(view.resource_id, resource.resource_id)
                    self.assertEqual(
                        view.generation,
                        resource.report.activated_generation,
                    )
                    health = view.health()
                    self.assertTrue(health.healthy)
                    self.assertTrue(health.exact_available)
                    self.assertEqual(
                        health.generation,
                        resource.report.activated_generation,
                    )
                    variants = view.exact_records(QUERY_SOURCE)
                    self.assertEqual(
                        tuple(record.target_raw for record in variants),
                        ("exact-variant", "context-variant"),
                    )
                    self.assertEqual(
                        tuple(record.record_id for record in variants),
                        (2, 1),
                    )

                with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                    _ = view.health()
                self.assertEqual(
                    raised.exception.code,
                    "STORE.QUERY_VIEW_EXPIRED",
                )

    def test_fixture_semantics_repeat_and_include_cross_resource_ties(
        self,
    ) -> None:
        scorer = SimilarityScorerV1()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = build_activated_canonical_fixture(root / "first")
            second = build_activated_canonical_fixture(root / "second")

            self.assertEqual(
                first.semantic_transcript(),
                second.semantic_transcript(),
            )
            self.assertEqual(
                first.resources[0].report.source_digest,
                first.resources[1].report.source_digest,
            )

            expected_scores = {
                HIGH_FUZZY_SOURCE: 0.8,
                BOUNDARY_SOURCE: 0.60,
                BELOW_THRESHOLD_SOURCE: 0.575,
                ONE_HUNDRED_FUZZY_SOURCE: 1.0,
            }
            observed_scores: list[dict[str, float]] = []
            for resource in first.resources:
                store = cast(
                    SQLiteTMStore,
                    cast(object, resource.handle.store),
                )
                self.assertIs(type(store), SQLiteTMStore)
                with store.query_lease() as view:
                    health = view.health()
                    records = view.records_by_id(
                        tuple(range(1, health.record_count + 1))
                    )
                source_scores = {
                    record.source_raw: scorer.score(
                        QUERY_SOURCE,
                        record.source_raw,
                    ).final_similarity
                    for record in records
                    if record.source_raw != QUERY_SOURCE
                }
                self.assertEqual(source_scores, expected_scores)
                observed_scores.append(source_scores)

            self.assertNotEqual(
                first.resources[0].resource_id,
                first.resources[1].resource_id,
            )
            self.assertEqual(
                observed_scores[0][HIGH_FUZZY_SOURCE],
                observed_scores[1][HIGH_FUZZY_SOURCE],
            )
            self.assertLess(
                observed_scores[0][BELOW_THRESHOLD_SOURCE],
                0.60,
            )


if __name__ == "__main__":
    unittest.main()
