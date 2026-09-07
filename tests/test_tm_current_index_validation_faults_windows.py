"""Adversarial checks for process-local current canonical index witnesses."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest import mock

import tm_sqlite_store
from tm_candidate_store_contracts import (
    CandidateProofIndexError, SQLiteStoreSchemaError,
)
from tm_content_attestation import ContentAttestationError
from tm_contracts import SourceBindingState
from tm_migration import TMMigrationService
from tm_sqlite_store import (
    ActivationPreparationError, ResourceStoreCoordinator, SQLiteStoreLifecycleError,
    SQLiteTMStore,
)
from tests.test_tm_current_index_validation_windows import _append, _changed_canonical
from tests.test_tm_portable_publication_windows import _fresh_completed_rehydrate


@contextmanager
def _capture_fault(boundary: str):
    """Keep actual rooted handles, and fail only the named terminal boundary."""
    real_capture = tm_sqlite_store._capture_platform_content_file
    observed = []

    class Capture:
        def __init__(self, actual):
            self.actual = actual
            self.reprove_count = 0
            self.closed = False

        def reprove(self):
            self.reprove_count += 1
            facts = self.actual.reprove()
            if (
                boundary == "initial_reprove" and self.reprove_count == 1
                or boundary == "terminal_reprove" and self.reprove_count == 2
            ):
                raise ContentAttestationError("CONTENT_ATTESTATION.FILE_UNSAFE")
            return facts

        def close(self):
            self.actual.close()
            self.closed = True
            if boundary == "close":
                raise OSError("injected capture close failure")

    def capture(*args, **kwargs):
        retained = Capture(real_capture(*args, **kwargs))
        observed.append(retained)
        return retained

    with mock.patch.object(
        tm_sqlite_store, "_capture_platform_content_file", side_effect=capture,
    ):
        yield observed


@unittest.skipUnless(os.name == "nt", "real Windows rooted file authority")
class CurrentIndexValidationFaultWindowsTests(unittest.TestCase):
    def test_same_byte_foreign_replacement_is_rejected_on_every_retry(self):
        with _changed_canonical() as (identity, coordinator, store):
            self.assertTrue(store.health().healthy)
            database = identity.canonical_sidecar_path
            before = database.read_bytes()
            replacement = database.with_name("foreign-retry.sqlite3")
            replacement.write_bytes(before)
            self.assertNotEqual(replacement.stat().st_ino, database.stat().st_ino)
            os.replace(replacement, database)
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError, "^STORE.ACTIVE_ATTESTATION_INVALID$",
                    ):
                        store.health()
                    self.assertIsNone(coordinator._current_index_validation)
            with store.query_lease() as query:
                for attempt in range(2):
                    with self.subTest(query_attempt=attempt):
                        with self.assertRaisesRegex(
                            SQLiteStoreSchemaError, "^STORE.ACTIVE_ATTESTATION_INVALID$",
                        ):
                            query.health()
                        self.assertIsNone(coordinator._current_index_validation)
            self.assertEqual(database.read_bytes(), before)

    def test_failed_health_revokes_concurrent_success_but_keeps_identity_anchor(self):
        with _changed_canonical() as (identity, coordinator, store):
            entered = threading.Event()
            release_failure = threading.Event()
            success_finished = threading.Event()
            failures, successes, unexpected = [], [], []
            real_validate = tm_sqlite_store.validate_candidate_proof_index

            def validate(*args, **kwargs):
                if threading.current_thread().name == "failing-health":
                    entered.set()
                    if not release_failure.wait(15):
                        raise AssertionError("failed health release was not signalled")
                    raise CandidateProofIndexError("CANDIDATE.PROOF_INVALID")
                return real_validate(*args, **kwargs)

            def failing_health():
                try:
                    store.health()
                except BaseException as error:
                    failures.append(error)

            def successful_health():
                try:
                    successes.append(store.health())
                except BaseException as error:
                    unexpected.append(error)
                finally:
                    success_finished.set()

            first = threading.Thread(target=failing_health, name="failing-health")
            second = threading.Thread(target=successful_health, name="successful-health")
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index", side_effect=validate,
            ):
                try:
                    first.start()
                    self.assertTrue(entered.wait(10))
                    second.start()
                    self.assertTrue(success_finished.wait(10))
                    self.assertEqual(unexpected, [])
                    self.assertEqual(len(successes), 1)
                    self.assertTrue(successes[0].healthy)
                    self.assertIsNotNone(coordinator._current_index_validation)
                    anchor = coordinator._current_index_identity
                    self.assertIsNotNone(anchor)
                finally:
                    release_failure.set()
                    first.join(15)
                    if second.ident is not None:
                        second.join(15)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], SQLiteStoreSchemaError)
            self.assertEqual(str(failures[0]), "STORE.CANDIDATE_INDEX_INVALID")
            self.assertIsNone(coordinator._current_index_validation)
            self.assertIs(coordinator._current_index_identity, anchor)
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index", wraps=real_validate,
            ) as validator:
                self.assertTrue(store.health().healthy)
                self.assertTrue(store.health().healthy)
            validator.assert_called_once()
            database = identity.canonical_sidecar_path
            replacement = database.with_name("foreign-after-concurrent-failure.sqlite3")
            replacement.write_bytes(database.read_bytes())
            self.assertNotEqual(replacement.stat().st_ino, database.stat().st_ino)
            os.replace(replacement, database)
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    with self.assertRaisesRegex(
                        SQLiteStoreSchemaError, "^STORE.ACTIVE_ATTESTATION_INVALID$",
                    ):
                        store.health()

    def test_concurrent_health_health_never_publishes_unfinished_validation(self):
        with _changed_canonical() as (_identity, coordinator, store):
            entered = threading.Event()
            release = threading.Event()
            second_entered = threading.Event()
            results, errors = [], []
            real_validate = tm_sqlite_store.validate_candidate_proof_index

            def validate(*args, **kwargs):
                if threading.current_thread().name == "first-health":
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("health release was not signalled")
                else:
                    second_entered.set()
                return real_validate(*args, **kwargs)

            def health():
                try:
                    results.append(store.health())
                except BaseException as error:
                    errors.append(error)

            first = threading.Thread(target=health, name="first-health")
            second = threading.Thread(target=health, name="second-health")
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index", side_effect=validate,
            ):
                try:
                    first.start()
                    self.assertTrue(entered.wait(10))
                    self.assertIsNone(coordinator._current_index_validation)
                    second.start()
                    self.assertTrue(second_entered.wait(10))
                finally:
                    release.set()
                    first.join(15)
                    if second.ident is not None:
                        second.join(15)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result.record_count for result in results], [4, 4])
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                side_effect=AssertionError("finished unchanged health must reuse"),
            ):
                self.assertTrue(store.health().healthy)

    def test_concurrent_health_append_rejects_bounded_then_retry_revalidates(self):
        with _changed_canonical() as (identity, _coordinator, store):
            entered = threading.Event()
            release = threading.Event()
            write_finished = threading.Event()
            health_errors, write_errors = [], []
            real_validate = tm_sqlite_store.validate_candidate_proof_index

            def validate(*args, **kwargs):
                entered.set()
                if not release.wait(15):
                    raise AssertionError("health release was not signalled")
                return real_validate(*args, **kwargs)

            def health():
                try:
                    store.health()
                except BaseException as error:
                    health_errors.append(error)

            def append():
                try:
                    _append(store, "concurrent")
                except BaseException as error:
                    write_errors.append(error)
                finally:
                    write_finished.set()

            before = identity.canonical_sidecar_path.read_bytes()
            reader = threading.Thread(target=health)
            writer = threading.Thread(target=append)
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index", side_effect=validate,
            ):
                try:
                    reader.start()
                    self.assertTrue(entered.wait(10))
                    writer.start()
                    self.assertTrue(write_finished.wait(10), "write did not reject within bound")
                    self.assertEqual(len(write_errors), 1)
                    # SQLite's Windows VFS reports the retained read handle's
                    # write-sharing rejection as READONLY, not BUSY. Preserve
                    # the existing error contract; do not relabel every
                    # READONLY cause as a transient health conflict.
                    self.assertIsInstance(write_errors[0], sqlite3.OperationalError)
                    self.assertEqual(write_errors[0].sqlite_errorcode, sqlite3.SQLITE_READONLY)
                    self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                finally:
                    release.set()
                    reader.join(15)
                    if writer.ident is not None:
                        writer.join(15)
            self.assertFalse(reader.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(health_errors, [])
            _append(store, "retry")
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index", wraps=real_validate,
            ) as validator:
                self.assertEqual(store.health().record_count, 5)
                self.assertEqual(store.health().record_count, 5)
            validator.assert_called_once()

    def test_same_size_record_or_posting_tamper_revalidates_and_rejects(self):
        mutations = {
            "record": "UPDATE tm_record SET source_raw='sXme' WHERE record_id=1",
            "posting": (
                "UPDATE tm_gram SET term_frequency=term_frequency+1 "
                "WHERE rowid=(SELECT min(rowid) FROM tm_gram)"
            ),
        }
        for role, statement in mutations.items():
            with self.subTest(role=role), _changed_canonical() as (
                identity, coordinator, store,
            ):
                self.assertTrue(store.health().healthy)
                database = identity.canonical_sidecar_path
                before = database.read_bytes()
                inode = database.stat().st_ino
                connection = sqlite3.connect(database)
                try:
                    self.assertEqual(connection.execute(statement).rowcount, 1)
                    connection.commit()
                finally:
                    connection.close()
                after = database.read_bytes()
                self.assertEqual(len(after), len(before))
                self.assertNotEqual(after, before)
                self.assertEqual(database.stat().st_ino, inode)
                with mock.patch.object(
                    tm_sqlite_store, "validate_candidate_proof_index",
                    wraps=tm_sqlite_store.validate_candidate_proof_index,
                ) as validator:
                    for _ in range(2):
                        with self.assertRaisesRegex(
                            SQLiteStoreSchemaError,
                            "^STORE.CANDIDATE_INDEX_INVALID$",
                        ):
                            store.health()
                        self.assertIsNone(coordinator._current_index_validation)
                self.assertEqual(validator.call_count, 2)
                self.assertEqual(database.read_bytes(), after)

    def test_validator_failure_does_not_create_a_reusable_result(self):
        with _changed_canonical() as (_identity, coordinator, store):
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                side_effect=CandidateProofIndexError("CANDIDATE.PROOF_INVALID"),
            ) as validator:
                with self.assertRaisesRegex(
                    SQLiteStoreSchemaError, "^STORE.CANDIDATE_INDEX_INVALID$",
                ):
                    store.health()
            validator.assert_called_once()
            self.assertIsNone(coordinator._current_index_validation)
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validator:
                self.assertTrue(store.health().healthy)
                self.assertTrue(store.health().healthy)
            validator.assert_called_once()

    def test_capture_failure_discards_old_or_unfinished_validation(self):
        for warm in (False, True):
            for boundary in ("initial_reprove", "terminal_reprove", "close"):
                with self.subTest(warm=warm, boundary=boundary):
                    with _changed_canonical() as (identity, coordinator, store):
                        if warm:
                            self.assertTrue(store.health().healthy)
                        before = identity.canonical_sidecar_path.read_bytes()
                        with (
                            _capture_fault(boundary) as captures,
                            mock.patch.object(
                                tm_sqlite_store, "validate_candidate_proof_index",
                                wraps=tm_sqlite_store.validate_candidate_proof_index,
                            ) as validator,
                        ):
                            with self.assertRaisesRegex(
                                SQLiteStoreSchemaError,
                                "^STORE.ACTIVE_ATTESTATION_INVALID$",
                            ):
                                store.health()
                        self.assertEqual(len(captures), 1)
                        self.assertTrue(captures[0].closed)
                        self.assertEqual(
                            captures[0].reprove_count,
                            1 if boundary == "initial_reprove" else 2,
                        )
                        self.assertEqual(
                            validator.call_count,
                            0 if warm or boundary == "initial_reprove" else 1,
                        )
                        self.assertIsNone(coordinator._current_index_validation)
                        with mock.patch.object(
                            tm_sqlite_store, "validate_candidate_proof_index",
                            wraps=tm_sqlite_store.validate_candidate_proof_index,
                        ) as validator:
                            self.assertTrue(store.health().healthy)
                            self.assertTrue(store.health().healthy)
                        validator.assert_called_once()
                        self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)

    def test_equal_generation_view_is_not_the_exact_validated_view(self):
        with _changed_canonical() as (_identity, coordinator, store):
            self.assertTrue(store.health().healthy)
            old_view = coordinator._view
            assert old_view is not None
            coordinator._view = replace(old_view)
            self.assertEqual(coordinator._view, old_view)
            self.assertIsNot(coordinator._view, old_view)
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validator:
                self.assertTrue(store.health().healthy)
                self.assertTrue(store.health().healthy)
            validator.assert_called_once()

    def test_sqlite_close_failure_does_not_publish_unfinished_validation(self):
        self._assert_sqlite_close_failure(warm=False)

    def test_sqlite_close_failure_revokes_previously_cached_validation(self):
        self._assert_sqlite_close_failure(warm=True)

    def _assert_sqlite_close_failure(self, *, warm):
        with _changed_canonical() as (_identity, coordinator, store):
            if warm:
                self.assertTrue(store.health().healthy)
            real_open = tm_sqlite_store._open_health_connection
            closed = []

            @contextmanager
            def failing_close(lease):
                with real_open(lease) as connection:
                    yield connection
                closed.append(True)
                raise OSError("injected SQLite close failure")

            with (
                mock.patch.object(
                    tm_sqlite_store, "_open_health_connection", new=failing_close,
                ),
                mock.patch.object(
                    tm_sqlite_store, "validate_candidate_proof_index",
                    wraps=tm_sqlite_store.validate_candidate_proof_index,
                ) as validator,
            ):
                with self.assertRaisesRegex(OSError, "injected SQLite close failure"):
                    store.health()
            self.assertEqual(validator.call_count, 0 if warm else 1)
            self.assertEqual(closed, [True])
            revoked = coordinator._current_index_validation is None
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validator:
                self.assertTrue(store.health().healthy)
                self.assertTrue(store.health().healthy)
            validator.assert_called_once()
            self.assertTrue(revoked, "failed health retained its previous index witness")

    def test_cold_validator_or_retained_database_failure_cannot_mint(self):
        for boundary in ("validator", "terminal_reprove", "close"):
            with self.subTest(boundary=boundary), _changed_canonical() as (
                identity, _old, _store,
            ):
                coordinator = ResourceStoreCoordinator(
                    canonical_store_id="store.primary", resource_identity=identity,
                )
                service = TMMigrationService(
                    resource_identity=identity, canonical_store_id="store.primary",
                    coordinator=coordinator,
                )
                before = identity.canonical_sidecar_path.read_bytes()
                validation_finished = []
                real_validate = tm_sqlite_store.validate_candidate_proof_index
                retained = []

                def validate(*args, **kwargs):
                    real_validate(*args, **kwargs)
                    validation_finished.append(True)
                    if boundary == "validator":
                        raise CandidateProofIndexError("CANDIDATE.PROOF_INVALID")

                class Database:
                    def __init__(self, actual):
                        self.actual = actual
                        self.closed = False
                        self.terminal_reproved = False

                    def content_facts(self):
                        facts = self.actual.content_facts()
                        if validation_finished:
                            self.terminal_reproved = True
                            if boundary == "terminal_reprove":
                                raise OSError("injected cold database reproof failure")
                        return facts

                    def close(self):
                        self.actual.close()
                        self.closed = True
                        if boundary == "close" and validation_finished:
                            raise OSError("injected cold database close failure")

                with service._acquire_initial_reservation() as reservation:
                    inputs = reservation.portable_runtime_inputs()
                    backend = inputs["platform"]
                    real_open = backend.open_regular

                    def open_regular(root, relative):
                        actual = real_open(root, relative)
                        if str(relative) != identity.canonical_sidecar_path.name:
                            return actual
                        database = Database(actual)
                        retained.append(database)
                        return database

                    with (
                        mock.patch.object(backend, "open_regular", side_effect=open_regular),
                        mock.patch.object(
                            tm_sqlite_store, "validate_candidate_proof_index",
                            side_effect=validate,
                        ) as validator,
                    ):
                        error_type = (
                            SQLiteStoreSchemaError if boundary == "validator"
                            else ActivationPreparationError
                        )
                        error_code = (
                            "STORE.CANDIDATE_INDEX_INVALID" if boundary == "validator"
                            else "ACTIVATION.RECOVERY_REQUIRED"
                        )
                        with self.assertRaisesRegex(error_type, f"^{error_code}$"):
                            coordinator.rehydrate_completed_portable_activation(**inputs)
                    validator.assert_called_once()
                    self.assertEqual(validation_finished, [True])
                    self.assertEqual(len(retained), 1)
                    self.assertTrue(retained[0].closed)
                    self.assertEqual(
                        retained[0].terminal_reproved, boundary != "validator",
                    )
                    self.assertIsNone(coordinator._current_index_validation)
                    self.assertIsNone(coordinator.current_generation)
                    self.assertEqual(coordinator.state, "ACTIVATING")
                    with mock.patch.object(
                        tm_sqlite_store, "validate_candidate_proof_index",
                        wraps=real_validate,
                    ) as validator:
                        outcome = coordinator.rehydrate_completed_portable_activation(**inputs)
                        self.assertEqual(outcome.action, "COMPLETED")
                        self.assertTrue(SQLiteTMStore.from_coordinator(coordinator).health().healthy)
                    validator.assert_called_once()
                self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)

    def test_foreign_coordinator_result_cannot_be_transplanted(self):
        with _changed_canonical() as (identity, first, store):
            self.assertTrue(store.health().healthy)
            second, outcome = _fresh_completed_rehydrate(identity)
            self.assertEqual(outcome.action, "COMPLETED")
            # Align every fact except the coordinator owner to isolate that guard.
            original = first._current_index_validation
            assert original is not None
            assert second._view is not None
            second._current_index_validation = replace(
                original, view=second._view,
            )
            other = SQLiteTMStore.from_coordinator(second)
            with mock.patch.object(
                tm_sqlite_store, "validate_candidate_proof_index",
                wraps=tm_sqlite_store.validate_candidate_proof_index,
            ) as validator:
                self.assertTrue(other.health().healthy)
                self.assertTrue(other.health().healthy)
            validator.assert_called_once()

    def test_expired_query_cannot_use_cached_health_or_reopen_database(self):
        with _changed_canonical() as (_identity, _coordinator, store):
            with store.query_lease() as query:
                self.assertTrue(query.health().healthy)
            with (
                mock.patch.object(tm_sqlite_store, "_open_health_connection") as opened,
                mock.patch.object(tm_sqlite_store, "_capture_platform_content_file") as capture,
                mock.patch.object(tm_sqlite_store, "validate_candidate_proof_index") as validator,
            ):
                with self.assertRaisesRegex(
                    SQLiteStoreLifecycleError, "^STORE.QUERY_VIEW_EXPIRED$",
                ):
                    query.health()
            opened.assert_not_called()
            capture.assert_not_called()
            validator.assert_not_called()

    def test_missing_source_pair_does_not_invalidate_current_index_result(self):
        for role in ("source", "manifest"):
            with self.subTest(role=role), _changed_canonical() as (
                identity, _coordinator, store,
            ):
                self.assertTrue(store.health().healthy)
                missing = (
                    identity.configured_jsonl_path if role == "source"
                    else identity.snapshot_manifest_path
                )
                missing.unlink()
                before = identity.canonical_sidecar_path.read_bytes()
                with mock.patch.object(
                    tm_sqlite_store, "validate_candidate_proof_index",
                    side_effect=AssertionError("unchanged current index must be reused"),
                ) as validator:
                    self.assertTrue(store.health().healthy)
                    with store.query_lease() as query:
                        self.assertEqual(query.health().record_count, 4)
                validator.assert_not_called()
                self.assertEqual(identity.canonical_sidecar_path.read_bytes(), before)
                self.assertIs(
                    store.source_binding_monitor.observe().state,
                    SourceBindingState.SOURCE_DIVERGED,
                )
                with mock.patch.object(
                    tm_sqlite_store, "validate_candidate_proof_index",
                    wraps=tm_sqlite_store.validate_candidate_proof_index,
                ) as validator:
                    self.assertIs(
                        store.health().source_binding_state,
                        SourceBindingState.SOURCE_DIVERGED,
                    )
                    self.assertTrue(store.health().healthy)
                validator.assert_called_once()

    def test_fresh_process_validates_current_database_once_before_reuse(self):
        with _changed_canonical() as (identity, _coordinator, store):
            self.assertTrue(store.health().healthy)
            code = """
from pathlib import Path
import sys
from unittest import mock
import tm_sqlite_store
from tm_contracts import CanonicalResourceIdentity
from tm_sqlite_store import SQLiteTMStore
from tests.test_tm_portable_publication_windows import _fresh_completed_rehydrate
identity = CanonicalResourceIdentity.from_configured_jsonl('tm.primary', Path(sys.argv[1]))
with mock.patch.object(tm_sqlite_store, 'validate_candidate_proof_index', wraps=tm_sqlite_store.validate_candidate_proof_index) as validator:
    coordinator, outcome = _fresh_completed_rehydrate(identity)
    assert outcome.action == 'COMPLETED'
    store = SQLiteTMStore.from_coordinator(coordinator)
    assert store.health().record_count == 4
    assert store.health().healthy
    assert validator.call_count == 1, validator.call_count
print('CURRENT_INDEX_CHILD_VERIFIED')
"""
            child = subprocess.run(
                [sys.executable, "-B", "-c", code, str(identity.configured_jsonl_path)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), "CURRENT_INDEX_CHILD_VERIFIED")


if __name__ == "__main__":
    unittest.main()
