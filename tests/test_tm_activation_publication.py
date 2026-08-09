"""Task 5.7 durable DB/manifest/generation publication tests."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
import tm_sqlite_store as store_module
from tests.test_tm_activation_journal import (
    SOURCE_BYTES,
    _existing_fixture,
    _first_prepared,
    _identity,
    _candidate,
    _registry,
)
from tm_sqlite_store import (
    ActivationPreparationError,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    _ActivationJournalPhase,
    _parse_activation_journal_bytes,
)
from tm_stage_sealer import StageSealError


def _phase(journal_path: Path) -> _ActivationJournalPhase:
    return _parse_activation_journal_bytes(
        journal_path.read_bytes(),
        expected_journal_path=journal_path,
    ).phase


class ActivationPublicationHappyPathTests(unittest.TestCase):
    def test_effects_are_durable_before_each_phase_and_token_consumption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity_value, coordinator, _sealed, prepared, journal = _first_prepared(root)
            registry = _registry(coordinator)
            events: list[str] = []
            real_replace_db = store_module._replace_activation_database
            real_validate_db = store_module._validate_replaced_activation_database
            real_receipt = store_module._publish_activation_receipt
            real_manifest = store_module._publish_activation_manifest
            real_validate_set = store_module._validate_published_activation_set
            real_advance = coordinator._advance_activation_journal_after_effect_locked
            real_consume = registry.consume

            def replace_db(*args: Any, **kwargs: Any) -> None:
                real_replace_db(*args, **kwargs)
                self.assertIs(
                    _phase(journal.journal_path),
                    _ActivationJournalPhase.PREPARED,
                )
                events.append("replace-db")

            def validate_db(*args: Any, **kwargs: Any) -> Any:
                value = real_validate_db(*args, **kwargs)
                events.append("validate-db")
                return value

            def publish_receipt(*args: Any, **kwargs: Any) -> None:
                self.assertIs(
                    _phase(journal.journal_path),
                    _ActivationJournalPhase.DB_REPLACED,
                )
                real_receipt(*args, **kwargs)
                events.append("publish-receipt")

            def publish_manifest(*args: Any, **kwargs: Any) -> None:
                self.assertIs(
                    _phase(journal.journal_path),
                    _ActivationJournalPhase.DB_REPLACED,
                )
                real_manifest(*args, **kwargs)
                events.append("publish-manifest")

            def validate_set(*args: Any, **kwargs: Any) -> Any:
                value = real_validate_set(*args, **kwargs)
                events.append("validate-set")
                return value

            def advance(
                preparation: Any,
                handle: Any,
                next_phase: _ActivationJournalPhase,
                **kwargs: Any,
            ) -> Any:
                value = real_advance(
                    preparation,
                    handle,
                    next_phase,
                    **kwargs,
                )
                events.append(f"journal-{next_phase.value}")
                return value

            def consume(token: Any) -> None:
                self.assertIs(
                    _phase(journal.journal_path),
                    _ActivationJournalPhase.GENERATION_PUBLISHED,
                )
                real_consume(token)
                events.append("consume-token")

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "tm_activation_recovery._replace_activation_database",
                        side_effect=replace_db,
                    )
                )
                stack.enter_context(
                    patch(
                        "tm_activation_recovery._validate_replaced_activation_database",
                        side_effect=validate_db,
                    )
                )
                stack.enter_context(
                    patch(
                        "tm_activation_recovery._publish_activation_receipt",
                        side_effect=publish_receipt,
                    )
                )
                stack.enter_context(
                    patch(
                        "tm_activation_recovery._publish_activation_manifest",
                        side_effect=publish_manifest,
                    )
                )
                stack.enter_context(
                    patch(
                        "tm_activation_recovery._validate_published_activation_set",
                        side_effect=validate_set,
                    )
                )
                stack.enter_context(
                    patch.object(
                        coordinator,
                        "_advance_activation_journal_after_effect_locked",
                        side_effect=advance,
                    )
                )
                stack.enter_context(
                    patch.object(registry, "consume", side_effect=consume)
                )
                coordinator.publish_activation(prepared, journal)

            self.assertLess(events.index("replace-db"), events.index("validate-db"))
            self.assertLess(
                events.index("validate-db"),
                events.index("journal-DB_REPLACED"),
            )
            self.assertLess(
                events.index("journal-DB_REPLACED"),
                events.index("publish-receipt"),
            )
            self.assertLess(
                events.index("publish-receipt"),
                events.index("publish-manifest"),
            )
            self.assertLess(
                events.index("publish-manifest"),
                events.index("journal-MANIFEST_PUBLISHED"),
            )
            self.assertLess(
                events.index("journal-MANIFEST_PUBLISHED"),
                events.index("journal-GENERATION_PUBLISHED"),
            )
            self.assertEqual(events[-1], "consume-token")

    def test_first_activation_publishes_one_complete_generation(self) -> None:
        for fts5_available in (True, False):
            with self.subTest(fts5_available=fts5_available):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        coordinator,
                        sealed,
                        prepared,
                        journal,
                    ) = _first_prepared(
                        root,
                        fts5_available=fts5_available,
                    )
                    candidate_db = journal._record.candidate_stage_db_path
                    candidate_manifest = (
                        journal._record.candidate_manifest_temp_path
                    )

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        generation = coordinator.publish_activation(
                            prepared,
                            journal,
                        )

                    self.assertEqual(generation, 0)
                    self.assertEqual(coordinator.current_generation, 0)
                    self.assertEqual(coordinator.state, "READY")
                    self.assertTrue(identity.canonical_sidecar_path.is_file())
                    self.assertTrue(identity.snapshot_manifest_path.is_file())
                    self.assertFalse(candidate_db.exists())
                    self.assertFalse(candidate_manifest.exists())
                    self.assertIs(
                        _registry(coordinator)._token_entry(
                            prepared._token
                        ).state,
                        contract_module.ActivationCapabilityState.CONSUMED,
                    )
                    disk = _parse_activation_journal_bytes(
                        journal.journal_path.read_bytes(),
                        expected_journal_path=journal.journal_path,
                    )
                    self.assertIs(
                        disk.phase,
                        _ActivationJournalPhase.GENERATION_PUBLISHED,
                    )
                    with coordinator._operation_lease() as lease:
                        self.assertEqual(
                            lease.stage.staged_db_path,
                            identity.canonical_sidecar_path,
                        )
                        self.assertEqual(lease.generation, 0)
                    connection = sqlite3.connect(identity.canonical_sidecar_path)
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT status FROM tm_snapshot_receipt"
                            ).fetchall(),
                            [("completed",)],
                        )
                        self.assertEqual(
                            connection.execute(
                                "SELECT COUNT(*) FROM tm_snapshot_binding"
                            ).fetchone(),
                            (1,),
                        )
                        self.assertEqual(
                            dict(connection.execute("SELECT key, value FROM tm_meta"))[
                                "activation_status"
                            ],
                            "ACTIVE",
                        )
                    finally:
                        connection.close()

    def test_existing_canonical_switches_from_prior_to_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")
            prepared = coordinator.activate(sealed)
            backups = tuple(
                (
                    asset.backup_path,
                    asset.evidence.backup_digest,
                )
                for asset in prepared._backup_assets
            )
            journal = coordinator.publish_prepared_activation(prepared)

            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                generation = coordinator.publish_activation(prepared, journal)

            self.assertEqual(generation, 1)
            self.assertEqual(coordinator.current_generation, 1)
            self.assertEqual(store.exact_records("same")[0].target_raw, "winner")
            self.assertEqual(store.exact_records("prior"), ())
            view = coordinator._view
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(
                view.stage.staged_db_path,
                identity.canonical_sidecar_path,
            )
            self.assertEqual(len(backups), 2)
            for backup_path, expected_digest in backups:
                self.assertTrue(backup_path.is_file())
                self.assertEqual(
                    hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                    expected_digest,
                )

    def test_canonical_prior_inode_is_replaced_and_preserved_in_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prior_view = coordinator._view
            self.assertIsNotNone(prior_view)
            assert prior_view is not None
            prior_path = prior_view.stage.staged_db_path
            os.replace(prior_path, identity.canonical_sidecar_path)
            prior_ref = store_module._canonical_activation_ref(
                identity,
                journal_id="prior",
            )
            coordinator._view = store_module._SQLiteGenerationView(
                stage=prior_ref,
                canonical_store_id=prior_view.canonical_store_id,
                generation=prior_view.generation,
                fts5_available=prior_view.fts5_available,
            )
            prior_digest = hashlib.sha256(
                identity.canonical_sidecar_path.read_bytes()
            ).hexdigest()
            self.assertEqual(store.exact_records("prior")[0].target_raw, "canonical")

            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                journal._record.prior_db_path,
                identity.canonical_sidecar_path,
            )
            prior_identity = journal._record.prior_db_identity
            candidate_identity = journal._record.candidate_stage_db_identity

            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    1,
                )

            canonical_observed = os.lstat(identity.canonical_sidecar_path)
            self.assertEqual(
                (canonical_observed.st_dev, canonical_observed.st_ino),
                candidate_identity,
            )
            self.assertNotEqual(candidate_identity, prior_identity)
            database_backup = next(
                asset
                for asset in prepared._backup_assets
                if asset.asset_kind == "DATABASE"
            )
            self.assertEqual(
                hashlib.sha256(database_backup.backup_path.read_bytes()).hexdigest(),
                prior_digest,
            )


class ActivationPublicationFailureTests(unittest.TestCase):
    def test_replace_failure_leaves_prepared_phase_and_unconsumed_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(root)
            with patch(
                "tm_activation_recovery._replace_activation_file",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            disk = _parse_activation_journal_bytes(
                journal.journal_path.read_bytes(),
                expected_journal_path=journal.journal_path,
            )
            self.assertIs(disk.phase, _ActivationJournalPhase.PREPARED)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIsNone(coordinator.current_generation)
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )
            with self.assertRaises(SQLiteStoreLifecycleError):
                with coordinator._operation_lease():
                    pass

    def test_database_parent_fsync_failure_does_not_advance_or_publish_view(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            with patch(
                "tm_activation_recovery._fsync_activation_directory",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)

            self.assertEqual(raised.exception.code, "ACTIVATION.DB_REPLACE_FAILED")
            self.assertIs(_phase(journal.journal_path), _ActivationJournalPhase.PREPARED)
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            self.assertIsNone(coordinator.current_generation)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_receipt_fsync_failure_stops_at_db_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            with patch(
                "tm_activation_recovery._fsync_activation_file",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)

            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECEIPT_PUBLICATION_FAILED",
            )
            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.DB_REPLACED,
            )
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertIsNone(coordinator.current_generation)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_manifest_replace_failure_stops_at_db_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            real_replace = store_module._replace_activation_file
            calls = 0

            def fail_second_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected")
                real_replace(source, destination)

            with patch(
                "tm_activation_recovery._replace_activation_file",
                side_effect=fail_second_replace,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)

            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.MANIFEST_PUBLICATION_FAILED",
            )
            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.DB_REPLACED,
            )
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertTrue(journal._record.candidate_manifest_temp_path.exists())
            self.assertIsNone(coordinator.current_generation)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_manifest_parent_fsync_failure_does_not_claim_manifest_phase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            real_fsync = store_module._fsync_activation_directory
            calls = 0

            def fail_manifest_fsync(path: Path) -> None:
                nonlocal calls
                calls += 1
                # DB parent, DB_REPLACED journal parent, then manifest parent.
                if calls == 3:
                    raise OSError("injected")
                real_fsync(path)

            with patch(
                "tm_activation_recovery._fsync_activation_directory",
                side_effect=fail_manifest_fsync,
            ), patch(
                "tm_activation_journal._fsync_activation_directory",
                side_effect=fail_manifest_fsync,
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.DB_REPLACED,
            )
            self.assertTrue(identity.snapshot_manifest_path.is_file())
            self.assertIsNone(coordinator.current_generation)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_mixed_completed_receipt_and_tampered_manifest_never_advance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            real_publish = store_module._publish_activation_manifest

            def publish_then_tamper(*args: Any, **kwargs: Any) -> None:
                real_publish(*args, **kwargs)
                identity.snapshot_manifest_path.write_bytes(b"{}")

            with patch(
                "tm_activation_recovery._publish_activation_manifest",
                side_effect=publish_then_tamper,
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.DB_REPLACED,
            )
            self.assertIsNone(coordinator.current_generation)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_index_tamper_after_replace_does_not_claim_db_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, _sealed, prepared, journal = _first_prepared(root)
            real_validate = store_module._validate_replaced_activation_database

            def tamper_then_validate(*args: Any, **kwargs: Any) -> Any:
                connection = sqlite3.connect(identity.canonical_sidecar_path)
                try:
                    connection.execute("DELETE FROM tm_gram")
                    connection.commit()
                finally:
                    connection.close()
                return real_validate(*args, **kwargs)

            with patch(
                "tm_activation_recovery._validate_replaced_activation_database",
                side_effect=tamper_then_validate,
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            self.assertIs(_phase(journal.journal_path), _ActivationJournalPhase.PREPARED)
            self.assertIsNone(coordinator.current_generation)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_final_revalidation_failure_retains_prior_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                _identity_value,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            real_validate = store_module._validate_published_activation_set
            calls = 0

            def fail_before_generation(*args: Any, **kwargs: Any) -> Any:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise ActivationPreparationError(
                        "ACTIVATION.ACTIVE_SET_INVALID",
                        retryable=False,
                    )
                return real_validate(*args, **kwargs)

            with patch(
                "tm_activation_recovery._validate_published_activation_set",
                side_effect=fail_before_generation,
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.MANIFEST_PUBLISHED,
            )
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )
            with self.assertRaises(SQLiteStoreLifecycleError):
                store.exact_records("prior")

    def test_final_journal_failure_rolls_back_unobservable_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                _identity_value,
                _store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            real_write = coordinator._write_activation_journal_locked

            def fail_final_write(
                record: Any,
                journal_path: Path,
                *,
                expected_final_identity: Any,
            ) -> Any:
                if record.phase is _ActivationJournalPhase.GENERATION_PUBLISHED:
                    raise ActivationPreparationError(
                        "ACTIVATION.JOURNAL_WRITE_FAILED",
                        retryable=True,
                    )
                return real_write(
                    record,
                    journal_path,
                    expected_final_identity=expected_final_identity,
                )

            with patch.object(
                coordinator,
                "_write_activation_journal_locked",
                side_effect=fail_final_write,
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.MANIFEST_PUBLISHED,
            )
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_token_consume_failure_occurs_only_after_final_durable_phase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _identity_value, coordinator, _sealed, prepared, journal = _first_prepared(root)
            registry = _registry(coordinator)
            with patch.object(
                registry,
                "consume",
                side_effect=StageSealError("SEALER.INJECTED"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)

            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.TOKEN_CONSUME_FAILED",
            )
            self.assertIs(
                _phase(journal.journal_path),
                _ActivationJournalPhase.GENERATION_PUBLISHED,
            )
            self.assertEqual(coordinator.current_generation, 0)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                registry._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )


class ActivationPublicationVisibilityTests(unittest.TestCase):
    def test_leases_are_blocked_until_manifest_and_generation_are_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                _identity_value,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []
            real_publish = store_module._publish_activation_manifest

            def blocked_publish(*args: Any, **kwargs: Any) -> None:
                entered.set()
                if not release.wait(5.0):
                    raise AssertionError("test manifest release timed out")
                real_publish(*args, **kwargs)

            def run() -> None:
                try:
                    with patch(
                        "tm_activation_recovery._publish_activation_manifest",
                        side_effect=blocked_publish,
                    ):
                        coordinator.publish_activation(prepared, journal)
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(5.0))
            reader_done = threading.Event()
            reader_results: list[tuple[str, ...]] = []

            def read_during_publication() -> None:
                reader_results.append(
                    tuple(record.target_raw for record in store.exact_records("same"))
                )
                reader_done.set()

            reader = threading.Thread(target=read_during_publication)
            reader.start()
            self.assertFalse(reader_done.wait(0.1))
            release.set()
            thread.join(5.0)
            reader.join(5.0)
            self.assertFalse(thread.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(reader_results, [("winner", "first")])
            self.assertEqual(store.exact_records("prior"), ())
            self.assertEqual(store.exact_records("same")[0].target_raw, "winner")
            self.assertEqual(coordinator.current_generation, 1)
            self.assertEqual(coordinator.state, "READY")

    def test_reopen_validation_failure_does_not_claim_db_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(root)
            with patch(
                "tm_activation_recovery._validate_replaced_activation_database",
                side_effect=ActivationPreparationError(
                    "ACTIVATION.DB_REOPEN_INVALID",
                    retryable=False,
                ),
            ):
                with self.assertRaises(ActivationPreparationError):
                    coordinator.publish_activation(prepared, journal)

            disk = _parse_activation_journal_bytes(
                journal.journal_path.read_bytes(),
                expected_journal_path=journal.journal_path,
            )
            self.assertIs(disk.phase, _ActivationJournalPhase.PREPARED)
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            self.assertFalse(journal._record.candidate_stage_db_path.exists())
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

    def test_tampered_manifest_is_rejected_before_database_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(root)
            manifest_temp = journal._record.candidate_manifest_temp_path
            payload = json.loads(manifest_temp.read_text(encoding="utf-8"))
            payload["snapshot_kind"] = "EXPLICIT_EXPORT"
            manifest_temp.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaises(ActivationPreparationError):
                coordinator.publish_activation(prepared, journal)

            self.assertFalse(identity.canonical_sidecar_path.exists())
            disk = _parse_activation_journal_bytes(
                journal.journal_path.read_bytes(),
                expected_journal_path=journal.journal_path,
            )
            self.assertIs(disk.phase, _ActivationJournalPhase.PREPARED)
            self.assertEqual(coordinator.state, "ACTIVATING")


if __name__ == "__main__":
    unittest.main()
