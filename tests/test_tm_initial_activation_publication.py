"""Task 2.2 first-generation publication and runtime-reopen contract."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    SourceBindingState,
    TMMatchType,
    TMQuery,
    TMResourceHandle,
)
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_retrieval import TMRetrievalService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"same","target":"winner"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _fixture(
    root: Path,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    TMMigrationService,
]:
    source = (root / "primary.jsonl").resolve()
    source.write_bytes(SOURCE_BYTES)
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        source,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    return identity, coordinator, service


class InitialActivationPublicationTests(unittest.TestCase):
    def test_published_binding_rewrite_cannot_pass_runtime_verification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, service = _fixture(root)
            real_publish = coordinator.publish_activation
            rebound_jsonl = (root / "foreign" / "rebound.jsonl").resolve()
            rebound_manifest = rebound_jsonl.with_name(
                f"{rebound_jsonl.name}.localcat-snapshot.json"
            )

            def publish_then_rebind(*args: Any) -> int:
                generation = real_publish(*args)
                active_store_path = coordinator.active_store_path
                assert active_store_path is not None
                with closing(
                    sqlite3.connect(active_store_path)
                ) as connection:
                    with connection:
                        connection.execute(
                            "UPDATE tm_snapshot_binding "
                            "SET configured_jsonl_path = ?, "
                            "manifest_path = ? WHERE binding_id = 1",
                            (str(rebound_jsonl), str(rebound_manifest)),
                        )
                        connection.execute(
                            "UPDATE tm_snapshot_receipt "
                            "SET destination_jsonl_path = ?, "
                            "destination_manifest_path = ?",
                            (str(rebound_jsonl), str(rebound_manifest)),
                        )
                return generation

            with (
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=publish_then_rebind,
                ),
                self.assertRaises(MigrationPreflightError) as raised,
            ):
                service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(
                raised.exception.error_code,
                "MIGRATION.INITIAL_RUNTIME_INVALID",
            )
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(
                coordinator.durable_activation_phase,
                "GENERATION_PUBLISHED",
            )
            reopened = SQLiteTMStore.from_coordinator(coordinator)
            health = reopened.health()
            self.assertTrue(health.healthy)
            self.assertTrue(health.exact_available)
            self.assertFalse(health.context_available)
            self.assertFalse(health.fuzzy_available)
            self.assertEqual(health.generation, 0)
            self.assertEqual(health.record_count, 3)
            self.assertEqual(health.diagnostic_codes, ())
            self.assertIsNotNone(health.snapshot_binding_digest)
            self.assertIs(
                health.source_binding_state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            self.assertEqual(
                tuple(
                    record.target_raw
                    for record in reopened.exact_records("same")
                ),
                ("winner", "first"),
            )

    def test_both_index_paths_publish_generation_zero_and_reopen_for_exact_query(
        self,
    ) -> None:
        for fts5_available, expected_index_kind in (
            (False, "GRAM_FALLBACK"),
            (True, "FTS5_TRIGRAM"),
        ):
            with self.subTest(index_kind=expected_index_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, coordinator, service = _fixture(root)
                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        outcome = service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertIs(type(outcome), MigrationReport)
                    report = outcome
                    assert isinstance(report, MigrationReport)
                    self.assertEqual(report.activated_generation, 0)
                    self.assertEqual(report.snapshot_receipt.record_count, 3)
                    self.assertEqual(report.migrated_count, 3)
                    self.assertEqual(report.variant_count, 1)
                    self.assertTrue(report.canonical_exact_available)
                    self.assertFalse(report.context_available)
                    self.assertFalse(report.fuzzy_available)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(
                        coordinator.durable_activation_phase,
                        "GENERATION_PUBLISHED",
                    )

                    store = SQLiteTMStore.from_coordinator(coordinator)
                    health = store.health()
                    revision = store.canonical_revision()
                    self.assertTrue(health.healthy)
                    self.assertTrue(health.exact_available)
                    self.assertFalse(health.context_available)
                    self.assertFalse(health.fuzzy_available)
                    self.assertEqual(health.index_kind, expected_index_kind)
                    self.assertEqual(health.generation, 0)
                    self.assertEqual(health.record_count, report.migrated_count)
                    self.assertIsNotNone(health.snapshot_binding_digest)
                    self.assertEqual(revision.resource_id, report.resource_id)
                    self.assertEqual(
                        revision.canonical_store_id,
                        report.canonical_store_id,
                    )
                    self.assertEqual(
                        revision.generation,
                        report.activated_generation,
                    )
                    self.assertEqual(
                        revision.record_count,
                        report.snapshot_receipt.record_count,
                    )
                    self.assertEqual(
                        tuple(
                            record.target_raw
                            for record in store.exact_records("same")
                        ),
                        ("winner", "first"),
                    )

                    query_report = TMRetrievalService().query(
                        (
                            TMResourceHandle(
                                resource_id=identity.resource_id,
                                store=store,
                                active=True,
                                lookup=True,
                                update=False,
                                order=0,
                            ),
                        ),
                        TMQuery(
                            query_source="same",
                            speaker_raw=None,
                            context_prev_raw=None,
                            context_next_raw=None,
                            minimum_similarity=0.60,
                            limit=10,
                            resource_order=(identity.resource_id,),
                        ),
                    )
                    self.assertEqual(query_report.resource_failures, ())
                    self.assertEqual(len(query_report.results), 1)
                    exact = query_report.results[0]
                    self.assertIs(exact.match_type, TMMatchType.EXACT)
                    self.assertEqual(exact.target, "winner")
                    self.assertEqual(exact.similarity, 1.0)

    def test_runtime_reopen_or_health_failure_cannot_return_success(self) -> None:
        for failed_port in ("from_coordinator", "health"):
            with self.subTest(failed_port=failed_port):
                with tempfile.TemporaryDirectory() as temporary:
                    identity, coordinator, service = _fixture(Path(temporary))
                    if failed_port == "from_coordinator":
                        runtime_patch = patch.object(
                            SQLiteTMStore,
                            "from_coordinator",
                            side_effect=RuntimeError("forced reopen failure"),
                        )
                    else:
                        runtime_patch = patch.object(
                            SQLiteTMStore,
                            "health",
                            side_effect=RuntimeError("forced health failure"),
                        )
                    with (
                        patch(
                            "tm_sqlite_store._probe_fts5",
                            return_value=False,
                        ),
                        runtime_patch,
                        self.assertRaises(MigrationPreflightError) as raised,
                    ):
                        service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertEqual(
                        raised.exception.error_code,
                        "MIGRATION.INITIAL_RUNTIME_REOPEN_FAILED",
                    )
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(
                        coordinator.durable_activation_phase,
                        "GENERATION_PUBLISHED",
                    )

    def test_returned_generation_must_equal_the_published_first_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            identity, coordinator, service = _fixture(Path(temporary))
            real_publish = coordinator.publish_activation

            def publish_with_false_return(*args: Any) -> int:
                real_publish(*args)
                return 1

            with (
                patch(
                    "tm_sqlite_store._probe_fts5",
                    return_value=False,
                ),
                patch.object(
                    coordinator,
                    "publish_activation",
                    side_effect=publish_with_false_return,
                ),
                self.assertRaises(MigrationPreflightError) as raised,
            ):
                service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(
                raised.exception.error_code,
                "MIGRATION.INITIAL_RUNTIME_INVALID",
            )
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(
                coordinator.durable_activation_phase,
                "GENERATION_PUBLISHED",
            )


if __name__ == "__main__":
    unittest.main()
