"""Current-generation index validation is reused, never publication authority."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tm_sqlite_store
from tm_candidate_store_contracts import SQLiteStoreSchemaError
from tm_contracts import TMRecordDraft
from tm_migration import MigrationReport
from tm_sqlite_store import SQLiteTMStore
from tests.test_tm_portable_publication_windows import (
    _fixture, _fresh_completed_rehydrate, _remove_long_quarantine,
)


def _append(store: SQLiteTMStore, suffix: str) -> None:
    store.append(TMRecordDraft(
        source_raw=f"current source {suffix}", target_raw=f"target {suffix}",
        speaker_raw=None, context_prev_raw=None, context_next_raw=None,
        file_source=None, provenance=(("source", "current-index-test"),),
    ))


@contextmanager
def _changed_canonical():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        identity, coordinator, service = _fixture(root)
        try:
            result = service.activate_initial(
                identity.configured_jsonl_path, identity.resource_id,
            )
            if type(result) is not MigrationReport:
                raise AssertionError(f"fixture activation: {type(result).__name__}")
            store = SQLiteTMStore.from_coordinator(coordinator)
            _append(store, "first")
            yield identity, coordinator, store
        finally:
            _remove_long_quarantine(root)


@unittest.skipUnless(os.name == "nt", "real Windows rooted file authority")
class CurrentIndexValidationWindowsTests(unittest.TestCase):
    def test_cold_hydration_and_two_health_calls_validate_current_bytes_once(self):
        with _changed_canonical() as (identity, _old, _store):
            before = identity.canonical_sidecar_path.read_bytes()
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validate:
                fresh, outcome = _fresh_completed_rehydrate(identity)
                self.assertEqual(outcome.action, "COMPLETED")
                store = SQLiteTMStore.from_coordinator(fresh)
                self.assertEqual(store.health().record_count, 4)
                with store.query_lease() as query:
                    self.assertEqual(query.health().record_count, 4)
            self.assertEqual(validate.call_count, 1)
            self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)

    def test_health_remembers_new_content_but_append_invalidates_it(self):
        with _changed_canonical() as (_identity, _coordinator, store):
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validate:
                self.assertEqual(store.health().record_count, 4)
                self.assertEqual(store.health().record_count, 4)
                self.assertEqual(validate.call_count, 1)
                _append(store, "second")
                after_write_checks = validate.call_count
                self.assertEqual(store.health().record_count, 5)
                self.assertEqual(store.health().record_count, 5)
                self.assertEqual(validate.call_count, after_write_checks + 1)

    def test_fresh_coordinator_must_do_its_own_full_validation(self):
        with _changed_canonical() as (identity, _old, _store):
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validate:
                for _ in range(2):
                    fresh, _outcome = _fresh_completed_rehydrate(identity)
                    self.assertTrue(SQLiteTMStore.from_coordinator(fresh).health().healthy)
            self.assertEqual(validate.call_count, 2)

    def test_same_byte_replacement_is_not_current_live_identity(self):
        with _changed_canonical() as (identity, _coordinator, store):
            self.assertTrue(store.health().healthy)
            replacement = identity.canonical_sidecar_path.with_name("foreign.sqlite3")
            replacement.write_bytes(identity.canonical_sidecar_path.read_bytes())
            os.replace(replacement, identity.canonical_sidecar_path)
            with self.assertRaisesRegex(
                SQLiteStoreSchemaError, "^STORE.ACTIVE_ATTESTATION_INVALID$",
            ):
                store.health()


if __name__ == "__main__":
    unittest.main()
