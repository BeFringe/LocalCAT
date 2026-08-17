"""Task 5.9 deterministic idempotent rollback tests.

The suite proves that an inconsistent pending activation (the durable
journal authenticates but the new DB/receipt/binding/manifest/effect closure
cannot be proven at any phase) is rolled back as one complete prior
authority instead of fail-stopped forever: an existing canonical generation
is restored from the journal-owned prior DB/manifest backups as a set, a
first activation keeps the configured JSONL as the legacy authority, every
journal-owned failed artifact is quarantined deterministically, the PREPARED
prior-closure terminal is retained, the pending journal is retired, and the
journal-owned backups are cleaned.  Authority-level faults (missing or
mutated backups, foreign/symlinked quarantine targets, a completed
generation, tampered source) still fail closed.  Crashes at every rollback
boundary resume idempotently with no duplicate quarantine, no second
generation, and no repeated token consumption.
"""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import tm_sqlite_store as store_module
from tests.test_tm_activation_journal import (
    SOURCE_BYTES,
    _existing_fixture,
    _first_prepared,
    _registry,
)
from tests.test_tm_activation_recovery import (
    _fresh,
    _recovered_report,
    _rewrite_phase,
)
from tm_sqlite_store import (
    ActivationPreparationError,
    ActivationRecoveryReport,
    MutableStageRef,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    SQLiteTMStore,
    _ActivationJournalHandle,
    _ActivationJournalPhase,
    _ActivationPreparation,
    _activation_journal_path,
    _activation_quarantine_directory,
    _activation_terminal_path,
)
from tm_contracts import ActivationCapabilityState


PREPARED = _ActivationJournalPhase.PREPARED
DB_REPLACED = _ActivationJournalPhase.DB_REPLACED
MANIFEST_PUBLISHED = _ActivationJournalPhase.MANIFEST_PUBLISHED
GENERATION_PUBLISHED = _ActivationJournalPhase.GENERATION_PUBLISHED


def _db_replaced_window(
    coordinator: ResourceStoreCoordinator,
    prepared: _ActivationPreparation,
    journal: _ActivationJournalHandle,
) -> None:
    """Drive the durable journal to DB_REPLACED and stop before the receipt."""

    with patch(
        "tm_activation_recovery._publish_activation_receipt",
        side_effect=OSError("injected"),
    ):
        try:
            coordinator.publish_activation(prepared, journal)
        except OSError:
            pass
        else:
            raise AssertionError("expected the injected receipt failure")


def _manifest_published_window(
    coordinator: ResourceStoreCoordinator,
    prepared: _ActivationPreparation,
    journal: _ActivationJournalHandle,
) -> None:
    """Drive the durable journal to MANIFEST_PUBLISHED with a full active set."""

    real_advance = (
        coordinator._advance_activation_journal_after_effect_locked
    )

    def fail_generation_journal(
        preparation: Any,
        handle: Any,
        next_phase: _ActivationJournalPhase,
        **kwargs: Any,
    ) -> Any:
        if next_phase is GENERATION_PUBLISHED:
            raise OSError("injected")
        return real_advance(preparation, handle, next_phase, **kwargs)

    with patch.object(
        coordinator,
        "_advance_activation_journal_after_effect_locked",
        side_effect=fail_generation_journal,
    ):
        try:
            coordinator.publish_activation(prepared, journal)
        except OSError:
            pass
        else:
            raise AssertionError("expected the injected journal failure")


def _quarantine_entries(
    identity: Any,
    record: Any,
) -> list[str]:
    quarantine_dir = _activation_quarantine_directory(identity, record)
    if not quarantine_dir.is_dir():
        return []
    return sorted(entry.name for entry in quarantine_dir.iterdir())


def _assert_first_activation_rolled_back(
    testcase: unittest.TestCase,
    identity: Any,
    record: Any,
    journal_path: Path,
    source_bytes: bytes,
    expected_phase: str,
    *,
    expected_quarantine: set[str],
) -> None:
    testcase.assertEqual(
        identity.configured_jsonl_path.read_bytes(),
        source_bytes,
    )
    testcase.assertFalse(identity.canonical_sidecar_path.exists())
    testcase.assertFalse(identity.snapshot_manifest_path.exists())
    testcase.assertFalse(journal_path.exists())
    terminal_path = _activation_terminal_path(identity)
    testcase.assertTrue(terminal_path.is_file())
    testcase.assertIs(
        store_module._parse_activation_journal_bytes(
            terminal_path.read_bytes(),
            expected_journal_path=journal_path,
        ).phase,
        PREPARED,
    )
    testcase.assertEqual(
        set(_quarantine_entries(identity, record)),
        expected_quarantine,
    )
    testcase.assertEqual(
        store_module._activation_journal_path(identity).exists(),
        False,
    )
    fresh = _fresh(identity)
    testcase.assertIsNone(fresh.recover_durable_activation())
    testcase.assertIsNone(fresh.current_generation)
    testcase.assertEqual(
        identity.configured_jsonl_path.read_bytes(),
        source_bytes,
    )


class RollbackFirstActivationTests(unittest.TestCase):
    def test_prepared_phase_rolls_back_to_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            with patch(
                "tm_activation_recovery._fsync_activation_directory",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.DB_REPLACE_FAILED",
            )
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            source_bytes = identity.configured_jsonl_path.read_bytes()
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertIsNone(recovered.current_generation)
            _assert_first_activation_rolled_back(
                self,
                identity,
                record,
                journal_path,
                source_bytes,
                "PREPARED",
                expected_quarantine={
                    identity.canonical_sidecar_path.name,
                    record.candidate_manifest_temp_path.name,
                },
            )

    def test_db_replaced_phase_rolls_back_to_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            _db_replaced_window(coordinator, prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            record.candidate_manifest_temp_path.write_bytes(b"tampered")
            source_bytes = identity.configured_jsonl_path.read_bytes()
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertIsNone(recovered.current_generation)
            _assert_first_activation_rolled_back(
                self,
                identity,
                record,
                journal_path,
                source_bytes,
                "DB_REPLACED",
                expected_quarantine={
                    identity.canonical_sidecar_path.name,
                    record.candidate_manifest_temp_path.name,
                },
            )
            quarantine_dir = _activation_quarantine_directory(
                identity,
                record,
            )
            connection = sqlite3.connect(
                quarantine_dir / identity.canonical_sidecar_path.name
            )
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM tm_snapshot_receipt"
                    ).fetchall(),
                    [("issued",)],
                )
            finally:
                connection.close()

    def test_manifest_published_phase_rolls_back_to_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            _manifest_published_window(coordinator, prepared, journal)
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)
            self.assertTrue(identity.canonical_sidecar_path.is_file())
            self.assertTrue(identity.snapshot_manifest_path.is_file())
            identity.canonical_sidecar_path.unlink()
            source_bytes = identity.configured_jsonl_path.read_bytes()
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="MANIFEST_PUBLISHED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertIsNone(recovered.current_generation)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertFalse(journal_path.exists())
            terminal_path = _activation_terminal_path(identity)
            self.assertTrue(terminal_path.is_file())
            self.assertEqual(
                set(_quarantine_entries(identity, record)),
                {identity.snapshot_manifest_path.name},
            )
            # The new canonical DB inode was externally deleted before the
            # rollback could quarantine it, so candidate retirement cannot
            # be proven from the deterministic quarantine directory: the
            # terminal replay fails closed instead of accepting bare
            # absence, and the legacy JSONL stays intact.
            fresh = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.QUARANTINE_MISSING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(fresh.state, "ACTIVATING")
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )

    def test_manifest_published_receipt_tamper_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            _manifest_published_window(coordinator, prepared, journal)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                connection.execute(
                    "UPDATE tm_snapshot_receipt "
                    "SET record_count = record_count + 1"
                )
                connection.commit()
            finally:
                connection.close()
            source_bytes = identity.configured_jsonl_path.read_bytes()
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="MANIFEST_PUBLISHED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertIsNone(recovered.current_generation)
            _assert_first_activation_rolled_back(
                self,
                identity,
                record,
                journal_path,
                source_bytes,
                "MANIFEST_PUBLISHED",
                expected_quarantine={
                    identity.canonical_sidecar_path.name,
                    identity.snapshot_manifest_path.name,
                },
            )
            quarantine_dir = _activation_quarantine_directory(
                identity,
                record,
            )
            connection = sqlite3.connect(
                quarantine_dir / identity.canonical_sidecar_path.name
            )
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT record_count FROM tm_snapshot_receipt"
                    ).fetchall(),
                    [(4,)],
                )
            finally:
                connection.close()

    def test_phase_only_generation_rewrite_without_attestation_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            _rewrite_phase(journal_path, GENERATION_PUBLISHED)
            malformed_bytes = journal_path.read_bytes()
            source_bytes = identity.configured_jsonl_path.read_bytes()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIsNone(recovered.current_generation)
            self.assertEqual(journal_path.read_bytes(), malformed_bytes)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertTrue(record.candidate_stage_db_path.is_file())
            self.assertTrue(record.candidate_manifest_temp_path.is_file())
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())

    def test_valid_generation_journal_missing_new_db_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            _manifest_published_window(coordinator, prepared, journal)
            manifest_record = store_module._parse_activation_journal_bytes(
                journal_path.read_bytes(),
                expected_journal_path=journal_path,
            )
            self.assertIs(manifest_record.phase, MANIFEST_PUBLISHED)
            self.assertIsNotNone(
                manifest_record.active_content_attestation
            )
            generation_record = replace(
                manifest_record,
                phase=GENERATION_PUBLISHED,
            )
            journal_path.write_text(
                store_module._serialize_activation_journal_record(
                    generation_record
                ),
                encoding="utf-8",
            )
            identity.canonical_sidecar_path.unlink()
            source_bytes = identity.configured_jsonl_path.read_bytes()

            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertIsNone(recovered.current_generation)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())
            self.assertEqual(
                set(_quarantine_entries(identity, generation_record)),
                {identity.snapshot_manifest_path.name},
            )


class RollbackExistingCanonicalTests(unittest.TestCase):
    def _assert_prior_restored(
        self,
        identity: Any,
        record: Any,
        prior_db_path: Path,
        prior_db_bytes: bytes,
        prior_manifest_bytes: bytes,
        journal_path: Path,
        expected_phase: str,
        generation: int,
    ) -> ResourceStoreCoordinator:
        recovered = _fresh(identity)
        self.assertEqual(
            _recovered_report(recovered),
            ActivationRecoveryReport(
                phase=expected_phase,
                action="ROLLED_BACK",
                generation=generation,
            ),
        )
        self.assertEqual(recovered.current_generation, generation)
        self.assertEqual(prior_db_path.read_bytes(), prior_db_bytes)
        self.assertEqual(
            record.prior_manifest_path.read_bytes(),
            prior_manifest_bytes,
        )
        self.assertFalse(journal_path.exists())
        self.assertTrue(_activation_terminal_path(identity).is_file())
        self.assertFalse(record.prior_db_backup_path.exists())
        self.assertFalse(record.prior_manifest_backup_path.exists())
        verify_store = SQLiteTMStore(
            MutableStageRef(
                stage_id="prior.verify",
                resource_identity=identity,
                staged_db_path=record.prior_db_path,
                manifest_temp_path=(
                    prior_db_path.parent / ".prior.verify.manifest.tmp"
                ),
            ),
            canonical_store_id="store.primary",
        )
        self.assertEqual(
            verify_store.exact_records("prior")[0].target_raw,
            "canonical",
        )
        return recovered

    def test_prepared_phase_restores_prior_pair_from_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            prior_manifest_path = record.prior_manifest_path
            assert prior_manifest_path is not None
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            prior_db_path.unlink()
            recovered = self._assert_prior_restored(
                identity,
                record,
                prior_db_path,
                prior_db_bytes,
                prior_manifest_bytes,
                journal_path,
                expected_phase="PREPARED",
                generation=0,
            )
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_db_replaced_phase_restores_prior_pair_from_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            prior_manifest_path = record.prior_manifest_path
            assert prior_manifest_path is not None
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            _db_replaced_window(coordinator, prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            record.candidate_manifest_temp_path.write_bytes(b"tampered")
            recovered = self._assert_prior_restored(
                identity,
                record,
                prior_db_path,
                prior_db_bytes,
                prior_manifest_bytes,
                journal_path,
                expected_phase="DB_REPLACED",
                generation=0,
            )
            self.assertEqual(recovered.current_generation, 0)
            self.assertFalse(identity.canonical_sidecar_path.exists())

    def test_manifest_published_phase_restores_prior_pair_from_backups(
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
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            prior_manifest_path = record.prior_manifest_path
            assert prior_manifest_path is not None
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            _manifest_published_window(coordinator, prepared, journal)
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)
            identity.canonical_sidecar_path.unlink()
            self._assert_prior_restored(
                identity,
                record,
                prior_db_path,
                prior_db_bytes,
                prior_manifest_bytes,
                journal_path,
                expected_phase="MANIFEST_PUBLISHED",
                generation=0,
            )

    def test_phase_only_generation_rewrite_keeps_prior_and_fails_closed(
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
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            prior_manifest_path = record.prior_manifest_path
            assert prior_manifest_path is not None
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            _rewrite_phase(journal_path, GENERATION_PUBLISHED)
            malformed_bytes = journal_path.read_bytes()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIsNone(recovered.current_generation)
            self.assertEqual(journal_path.read_bytes(), malformed_bytes)
            self.assertEqual(prior_db_path.read_bytes(), prior_db_bytes)
            self.assertEqual(
                prior_manifest_path.read_bytes(),
                prior_manifest_bytes,
            )
            self.assertTrue(record.candidate_stage_db_path.is_file())
            self.assertTrue(record.candidate_manifest_temp_path.is_file())
            self.assertEqual(
                _quarantine_entries(identity, record),
                [],
            )


class RollbackFailStopTests(unittest.TestCase):
    def test_missing_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            journal_bytes = journal_path.read_bytes()
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_path.unlink()
            prior_db_backup_path = record.prior_db_backup_path
            assert prior_db_backup_path is not None
            prior_db_backup_path.unlink()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(journal_path.read_bytes(), journal_bytes)

    def test_mutated_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            journal_bytes = journal_path.read_bytes()
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_path.unlink()
            prior_db_backup_path = record.prior_db_backup_path
            assert prior_db_backup_path is not None
            prior_db_backup_path.write_bytes(b"mutated backup")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_BACKUP_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(journal_path.read_bytes(), journal_bytes)

    def test_hardlinked_backup_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            journal_bytes = journal_path.read_bytes()
            prior_db_path = record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            prior_db_path.unlink()
            backup_path = record.prior_db_backup_path
            assert backup_path is not None
            other = backup_path.parent / "backup-hardlink.bak"
            os.link(backup_path, other)
            try:
                recovered = _fresh(identity)
                with self.assertRaises(ActivationPreparationError) as raised:
                    recovered.recover_durable_activation()
                self.assertEqual(
                    raised.exception.code,
                    "ACTIVATION.RECOVERY_BACKUP_INVALID",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(recovered.state, "ACTIVATING")
                self.assertEqual(journal_path.read_bytes(), journal_bytes)
            finally:
                other.unlink()

    def test_foreign_manifest_final_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                _store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            journal_bytes = journal_path.read_bytes()
            manifest_path = identity.snapshot_manifest_path
            manifest_path.unlink()
            os.symlink(identity.configured_jsonl_path, manifest_path)
            try:
                recovered = _fresh(identity)
                with self.assertRaises(ActivationPreparationError) as raised:
                    recovered.recover_durable_activation()
                self.assertEqual(
                    raised.exception.code,
                    "ACTIVATION.QUARANTINE_FOREIGN",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(recovered.state, "ACTIVATING")
                self.assertTrue(os.path.islink(manifest_path))
                self.assertEqual(journal_path.read_bytes(), journal_bytes)
            finally:
                manifest_path.unlink()

    def test_rollback_refuses_completed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            journal_path = journal.journal_path
            journal_bytes = journal_path.read_bytes()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.rollback_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.ROLLBACK_COMPLETED_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            fresh = _fresh(identity)
            self.assertEqual(
                _recovered_report(fresh),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)


class RollbackCrashResumeTests(unittest.TestCase):
    def _existing_prior_missing_base(
        self,
        root: Path,
    ) -> tuple[Any, Any, Any, Path, bytes]:
        (
            identity,
            store,
            coordinator,
            _stage,
            sealed,
        ) = _existing_fixture(root, fts5_available=True)
        prepared = coordinator.activate(sealed)
        journal = coordinator.publish_prepared_activation(prepared)
        record = journal._record
        prior_db_path = record.prior_db_path
        assert prior_db_path is not None
        prior_db_bytes = prior_db_path.read_bytes()
        prior_db_path.unlink()
        return identity, store, record, journal.journal_path, prior_db_bytes

    def test_quarantine_fsync_crash_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                record,
                journal_path,
                prior_db_bytes,
            ) = self._existing_prior_missing_base(root)
            quarantine_dir = _activation_quarantine_directory(
                identity,
                record,
            )
            recovered = _fresh(identity)

            def fail_quarantine_fsync(path: Path) -> None:
                if path == quarantine_dir.parent:
                    raise OSError("injected quarantine fsync")

            with patch(
                "tm_activation_journal._fsync_activation_directory",
                side_effect=fail_quarantine_fsync,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.QUARANTINE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertTrue(journal_path.exists())
            report = _recovered_report(_fresh(identity))
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(
                record.prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertFalse(journal_path.exists())
            self.assertEqual(
                len(_quarantine_entries(identity, record)),
                2,
            )

    def test_restore_fsync_crash_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                record,
                journal_path,
                prior_db_bytes,
            ) = self._existing_prior_missing_base(root)
            recovered = _fresh(identity)
            with patch(
                "tm_activation_recovery._fsync_recovery_backup",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.ROLLBACK_RESTORE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertTrue(journal_path.exists())
            report = _recovered_report(_fresh(identity))
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(
                record.prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertFalse(journal_path.exists())

    def test_terminal_write_crash_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                record,
                journal_path,
                prior_db_bytes,
            ) = self._existing_prior_missing_base(root)
            recovered = _fresh(identity)
            with patch.object(
                ResourceStoreCoordinator,
                "_write_activation_terminal_locked",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    recovered.recover_durable_activation()
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertTrue(journal_path.exists())
            report = _recovered_report(_fresh(identity))
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(
                record.prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())

    def test_journal_retirement_crash_resumes_via_rollback_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                record,
                journal_path,
                prior_db_bytes,
            ) = self._existing_prior_missing_base(root)
            recovered = _fresh(identity)
            with patch(
                "tm_activation_recovery._remove_owned_activation_journal_final",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    recovered.recover_durable_activation()
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertTrue(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())
            report = _recovered_report(_fresh(identity))
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(
                record.prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())
            self.assertEqual(
                len(_quarantine_entries(identity, record)),
                2,
            )

    def test_backup_cleanup_crash_resumes_via_terminal_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                record,
                journal_path,
                prior_db_bytes,
            ) = self._existing_prior_missing_base(root)
            recovered = _fresh(identity)
            with patch(
                "tm_activation_journal._unlink_recovery_backup",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())
            report = _recovered_report(_fresh(identity))
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(
                record.prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertFalse(record.prior_db_backup_path.exists())
            self.assertFalse(record.prior_manifest_backup_path.exists())


class RollbackEntryPointTests(unittest.TestCase):
    def test_repeated_rollback_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            journal_path = journal.journal_path
            record = journal._record
            prior_db_path = record.prior_db_path
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            source_bytes = identity.configured_jsonl_path.read_bytes()
            prior_db_path.unlink()
            recovered = _fresh(identity)
            self.assertEqual(
                recovered.rollback_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            terminal_identity = os.lstat(
                _activation_terminal_path(identity)
            ).st_ino
            quarantine_entries = _quarantine_entries(identity, record)
            self.assertEqual(
                recovered.rollback_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(
                os.lstat(_activation_terminal_path(identity)).st_ino,
                terminal_identity,
            )
            self.assertEqual(
                _quarantine_entries(identity, record),
                quarantine_entries,
            )
            self.assertEqual(
                prior_db_path.read_bytes(),
                prior_db_bytes,
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )

    def test_rollback_from_activating_cancels_live_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            _db_replaced_window(coordinator, prepared, journal)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertEqual(
                _registry(coordinator)
                ._token_entry(prepared._token)
                .state,
                ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertEqual(
                coordinator.rollback_durable_activation(),
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="ROLLED_BACK",
                    generation=None,
                ),
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(
                _registry(coordinator)
                ._token_entry(prepared._token)
                .state,
                ActivationCapabilityState.CANCELLED,
            )
            self.assertFalse(journal_path.exists())
            fresh = _fresh(identity)
            self.assertIsNone(fresh.recover_durable_activation())
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )

    def test_rollback_without_authority_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            source_bytes = identity.configured_jsonl_path.read_bytes()
            journal_path.unlink()
            recovered = _fresh(identity)
            self.assertIsNone(recovered.rollback_durable_activation())
            self.assertEqual(recovered.state, "READY")
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )

    def test_operations_gated_until_rollback_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                identity,
                store,
                coordinator,
                _stage,
                sealed,
            ) = _existing_fixture(root, fts5_available=True)
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            record = journal._record
            prior_db_path = record.prior_db_path
            assert prior_db_path is not None
            prior_db_bytes = prior_db_path.read_bytes()
            prior_db_path.unlink()
            recovered = _fresh(identity)
            with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                with recovered._operation_lease():
                    pass
            self.assertEqual(
                raised.exception.code,
                "STORE.CANONICAL_UNAVAILABLE",
            )
            self.assertEqual(
                recovered.rollback_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            with recovered._operation_lease():
                pass
            self.assertEqual(recovered.current_generation, 0)
            self.assertEqual(
                prior_db_path.read_bytes(),
                prior_db_bytes,
            )


class RollbackReportTests(unittest.TestCase):
    def test_rolled_back_report_is_code_only(self) -> None:
        report = ActivationRecoveryReport(
            phase="GENERATION_PUBLISHED",
            action="ROLLED_BACK",
            generation=None,
        )
        rendered = repr(report)
        self.assertNotIn("/", rendered)
        self.assertNotIn(".jsonl", rendered)
        self.assertNotIn(".sqlite3", rendered)
        self.assertNotIn("token", rendered.lower())
        for bad_action in ("ROLLED_BACKX", "ROLLED_FORWARD", ""):
            with self.assertRaises((TypeError, ValueError)):
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action=bad_action,
                    generation=None,
                )
        with self.assertRaises((TypeError, ValueError)):
            ActivationRecoveryReport(
                phase="PREPARED",
                action="ROLLED_BACK",
                generation=-1,
            )


def _phase(journal_path: Path) -> _ActivationJournalPhase:
    return store_module._parse_activation_journal_bytes(
        journal_path.read_bytes(),
        expected_journal_path=journal_path,
    ).phase


if __name__ == "__main__":
    unittest.main()
