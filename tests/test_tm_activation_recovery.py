"""Task 5.8 idempotent activation recovery and Task 5.9 rollback tests.

The suite drives every durable journal phase to a crash window, rebuilds a
fresh coordinator over the same resource identity, and proves that recovery
re-checks the new DB/receipt/manifest/journal/token closure from disk alone
and publishes each next journal phase only after its matching effect is
durable and revalidated: PREPARED cancels safely and cleans journal-owned
backups, DB_REPLACED -> MANIFEST_PUBLISHED -> GENERATION_PUBLISHED advances
monotonically, the terminal GENERATION_PUBLISHED journal is retained as the
durable consumed marker and replayed by fresh coordinators without a second
generation or repeated token consumption, no-journal discovery re-proves the
completed canonical generation (or the unchanged prior/legacy state), and
authority-level faults (tampered journal/terminal/source, foreign files,
backup or cleanup faults) fail-stop in ACTIVATING with durable journal
evidence.  Task 5.9 turns the four new-set closure faults (unprovable
DB/manifest/receipt/effect at any pending phase, missing prior pair) into a
deterministic rollback: the journal-owned backups restore the prior
DB/manifest pair as one set (or a first activation keeps the legacy JSONL),
failed artifacts are quarantined, the PREPARED prior-closure terminal is
retained, and the pending journal is retired idempotently.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_contracts as contract_module
import tm_sqlite_store as store_module
from tests.test_tm_activation_journal import (
    SOURCE_BYTES,
    _draft,
    _existing_fixture,
    _first_prepared,
    _identity,
    _registry,
)
from tm_contracts import (
    SNAPSHOT_MANIFEST_VERSION,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
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
    _activation_journal_temp_path,
    _parse_activation_journal_bytes,
    _serialize_activation_journal_record,
    initialize_stage_schema,
)
from tm_stage_sealer import StageSealer
from tm_sqlite_store import (
    ActivationPreparationError,
    ActivationRecoveryReport,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    _ActivationJournalPhase,
    _ActivationPreparation,
    _activation_journal_path,
    _activation_journal_temp_path,
    _parse_activation_journal_bytes,
    _serialize_activation_journal_record,
)


PREPARED = _ActivationJournalPhase.PREPARED
DB_REPLACED = _ActivationJournalPhase.DB_REPLACED
MANIFEST_PUBLISHED = _ActivationJournalPhase.MANIFEST_PUBLISHED
GENERATION_PUBLISHED = _ActivationJournalPhase.GENERATION_PUBLISHED


def _fresh(identity: contract_module.CanonicalResourceIdentity):
    return ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )


def _recovered_report(
    coordinator: ResourceStoreCoordinator,
) -> ActivationRecoveryReport:
    report = coordinator.recover_durable_activation()
    assert report is not None
    return report


def _phase(journal_path: Path) -> _ActivationJournalPhase:
    return _parse_activation_journal_bytes(
        journal_path.read_bytes(),
        expected_journal_path=journal_path,
    ).phase


def _rewrite_phase(
    journal_path: Path,
    phase: _ActivationJournalPhase,
) -> None:
    """Rewrite one valid journal with a new phase and a fresh digest."""

    record = _parse_activation_journal_bytes(
        journal_path.read_bytes(),
        expected_journal_path=journal_path,
    )
    journal_path.write_text(
        _serialize_activation_journal_record(replace(record, phase=phase)),
        encoding="utf-8",
    )


def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(
        connection.execute("SELECT key, value FROM tm_meta").fetchall()
    )


def _terminal_after_completion(journal_path: Path) -> None:
    """Assert the durable terminal protocol postconditions after completion."""

    assert _phase(journal_path) is GENERATION_PUBLISHED
    assert journal_path.is_file()
    assert not _activation_journal_temp_path(journal_path).exists()


def _second_sealed(
    coordinator: ResourceStoreCoordinator,
    identity: contract_module.CanonicalResourceIdentity,
    root: Path,
):
    """Build one fresh sealed stage at new paths for a second activation.

    The migration service refuses to build over an already-activated
    resource and the coordinator registry refuses to re-seal the same
    deterministic migration stage paths, so the second candidate is built
    manually at distinct paths with the same source closure.
    """

    stage = MutableStageRef(
        stage_id="stage.second",
        resource_identity=identity,
        staged_db_path=(root / ".localcat-second.sqlite3").resolve(),
        manifest_temp_path=(root / ".localcat-second.manifest.tmp").resolve(),
    )
    initialize_stage_schema(stage, canonical_store_id="store.primary")
    store = SQLiteTMStore(
        stage,
        canonical_store_id="store.primary",
    )
    source_digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
    store.append_batch(
        batch_id=f"migration.{source_digest}",
        kind="migration",
        drafts=(
            _migration_draft("same", "first"),
            _migration_draft("same", "winner"),
            _migration_draft("other", "value"),
        ),
        legacy_line_nos=(1, 2, 3),
        source_digest=source_digest,
        source_path=identity.configured_jsonl_path,
        duplicate_source_count=1,
    )
    revision = store.canonical_revision()
    receipt = SnapshotReceipt(
        snapshot_id=f"snapshot.migration.{source_digest[:24]}",
        resource_id=identity.resource_id,
        canonical_store_id="store.primary",
        exported_revision=revision.head_revision,
        jsonl_digest=source_digest,
        record_count=revision.record_count,
    )
    store.register_issued_snapshot_receipt(
        receipt,
        destination_jsonl_path=identity.configured_jsonl_path,
        destination_manifest_path=identity.snapshot_manifest_path,
    )
    manifest = SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=SnapshotKind.MIGRATION_SOURCE,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )
    stage.manifest_temp_path.write_bytes(contract_to_json(manifest).encode())
    return StageSealer(
        registry=coordinator.sealed_registry,
        canonical_store_id="store.primary",
    ).seal(
        stage,
        expected_prior_generation=0,
    )


def _migration_draft(source: str, target: str) -> contract_module.TMRecordDraft:
    """One migration-kind draft with the sealer's expected provenance."""

    return contract_module.TMRecordDraft(
        source_raw=source,
        target_raw=target,
        speaker_raw=None,
        context_prev_raw=None,
        context_next_raw=None,
        file_source=None,
        provenance=(("source", "legacy-jsonl"),),
    )


class ActivationRecoveryCancelTests(unittest.TestCase):
    def test_first_activation_prepared_cancel_restores_no_view(self) -> None:
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
                    journal_path = journal.journal_path
                    candidate_db = journal._record.candidate_stage_db_path
                    candidate_manifest = (
                        journal._record.candidate_manifest_temp_path
                    )

                    recovered = _fresh(identity)
                    self.assertIsNone(recovered.current_generation)
                    with self.assertRaises(SQLiteStoreLifecycleError):
                        with recovered._operation_lease():
                            pass

                    with patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=fts5_available,
                    ):
                        report = recovered.recover_durable_activation()
                    self.assertEqual(
                        report,
                        ActivationRecoveryReport(
                            phase="PREPARED",
                            action="CANCELLED",
                            generation=None,
                        ),
                    )
                    self.assertEqual(recovered.state, "READY")
                    self.assertIsNone(recovered.current_generation)
                    self.assertFalse(journal_path.exists())
                    self.assertFalse(
                        identity.canonical_sidecar_path.exists()
                    )
                    self.assertFalse(
                        identity.snapshot_manifest_path.exists()
                    )
                    self.assertTrue(candidate_db.is_file())
                    self.assertTrue(candidate_manifest.is_file())
                    self.assertEqual(
                        identity.configured_jsonl_path.read_bytes(),
                        SOURCE_BYTES,
                    )
                    self.assertIsNone(recovered.recover_durable_activation())
                    second_fresh = _fresh(identity)
                    self.assertIsNone(
                        second_fresh.recover_durable_activation()
                    )
                    self.assertEqual(second_fresh.state, "READY")
                    self.assertIsNone(second_fresh.current_generation)

    def test_existing_canonical_prepared_cancel_restores_prior_generation(
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
            prior_db_path = journal._record.prior_db_path
            self.assertIsNotNone(prior_db_path)
            assert prior_db_path is not None
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            prior_bytes = prior_db_path.read_bytes()
            prior_manifest_bytes = identity.snapshot_manifest_path.read_bytes()

            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)

            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            self.assertFalse(journal_path.exists())
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertEqual(prior_db_path.read_bytes(), prior_bytes)
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                prior_manifest_bytes,
            )
            with recovered._operation_lease() as lease:
                self.assertEqual(lease.stage.staged_db_path, prior_db_path)
                self.assertEqual(lease.generation, 0)
            connection = sqlite3.connect(prior_db_path)
            try:
                rows = connection.execute(
                    "SELECT source_raw, target_raw FROM tm_record "
                    "ORDER BY record_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [("prior", "canonical")])
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            terminal_path = store_module._activation_terminal_path(identity)
            self.assertTrue(terminal_path.is_file())
            terminal_bytes = terminal_path.read_bytes()
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            second_fresh = _fresh(identity)
            self.assertEqual(
                second_fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(second_fresh.state, "READY")
            self.assertEqual(second_fresh.current_generation, 0)
            with second_fresh._operation_lease() as lease:
                self.assertEqual(lease.stage.staged_db_path, prior_db_path)
                self.assertEqual(lease.generation, 0)
            connection = sqlite3.connect(prior_db_path)
            try:
                rows = connection.execute(
                    "SELECT source_raw, target_raw FROM tm_record "
                    "ORDER BY record_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [("prior", "canonical")])
            self.assertEqual(prior_db_path.read_bytes(), prior_bytes)
            third_fresh = _fresh(identity)
            self.assertEqual(
                third_fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(third_fresh.current_generation, 0)
            self.assertEqual(terminal_path.read_bytes(), terminal_bytes)
            self.assertFalse(journal_path.exists())
            # the terminal replay never resumed or consumed the token
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertEqual(store.coordinator.current_generation, 0)


class ActivationRecoveryCompletionTests(unittest.TestCase):
    def test_first_activation_db_replaced_recovery_publishes_one_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertTrue(
                journal._record.candidate_manifest_temp_path.is_file()
            )
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )

            recovered = _fresh(identity)
            self.assertIsNone(recovered.current_generation)
            with self.assertRaises(SQLiteStoreLifecycleError):
                with recovered._operation_lease():
                    pass

            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            _terminal_after_completion(journal_path)
            self.assertTrue(identity.snapshot_manifest_path.is_file())
            self.assertFalse(
                journal._record.candidate_manifest_temp_path.exists()
            )
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
                meta = _meta(connection)
                self.assertEqual(meta["activation_status"], "ACTIVE")
                self.assertEqual(meta["generation"], "0")
                self.assertIn("activation_digest", meta)
                winners = connection.execute(
                    "SELECT source_raw, target_raw FROM tm_record "
                    "ORDER BY record_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(len(winners), 3)
            self.assertIn(("same", "winner"), winners)
            self.assertIn(("other", "value"), winners)
            with recovered._operation_lease() as lease:
                self.assertEqual(
                    lease.stage.staged_db_path,
                    identity.canonical_sidecar_path,
                )
                self.assertEqual(lease.generation, 0)
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)

    def test_second_fresh_coordinator_rehydrates_after_terminal_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            first = _fresh(identity)
            self.assertEqual(
                first.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            _terminal_after_completion(journal_path)
            second = _fresh(identity)
            self.assertEqual(
                second.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.state, "READY")
            self.assertEqual(second.current_generation, 0)
            with second._operation_lease() as lease:
                self.assertEqual(lease.generation, 0)
            third = _fresh(identity)
            self.assertEqual(
                third.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(third.current_generation, 0)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone(),
                    (1,),
                )
            finally:
                connection.close()

    def test_first_activation_db_replaced_after_receipt_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_manifest",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM tm_snapshot_receipt"
                    ).fetchall(),
                    [("completed",)],
                )
                meta = _meta(connection)
                self.assertEqual(meta["activation_status"], "ACTIVE")
            finally:
                connection.close()
            self.assertFalse(identity.snapshot_manifest_path.exists())

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertTrue(identity.snapshot_manifest_path.is_file())
            _terminal_after_completion(journal_path)
            self.assertEqual(recovered.current_generation, 0)
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )

    def test_existing_canonical_db_replaced_replaces_prior_manifest(
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            with patch(
                "tm_sqlite_store._publish_activation_manifest",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertTrue(identity.snapshot_manifest_path.is_file())

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(recovered.current_generation, 1)
            _terminal_after_completion(journal_path)
            observed = os.lstat(identity.snapshot_manifest_path)
            self.assertEqual(
                (observed.st_dev, observed.st_ino),
                journal._record.candidate_manifest_temp_identity,
            )
            self.assertFalse(
                journal._record.candidate_manifest_temp_path.exists()
            )
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            self.assertFalse(
                list(root.glob("*.localcat-recovery.*.bak"))
            )
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                meta = _meta(connection)
                self.assertEqual(meta["activation_status"], "ACTIVE")
                self.assertEqual(meta["generation"], "1")
                self.assertIn("activation_digest", meta)
            finally:
                connection.close()
            with recovered._operation_lease() as lease:
                self.assertEqual(
                    lease.stage.staged_db_path,
                    identity.canonical_sidecar_path,
                )
                self.assertEqual(lease.generation, 1)
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )

    def test_manifest_published_recovery_publishes_unique_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            real_advance = (
                coordinator._advance_activation_journal_after_effect_locked
            )

            def fail_generation_journal(
                preparation: _ActivationPreparation,
                handle: _ActivationJournalHandle,
                next_phase: _ActivationJournalPhase,
                **kwargs: Any,
            ) -> Any:
                if next_phase is GENERATION_PUBLISHED:
                    raise OSError("injected")
                return real_advance(
                    preparation,
                    handle,
                    next_phase,
                    **kwargs,
                )

            with patch.object(
                coordinator,
                "_advance_activation_journal_after_effect_locked",
                side_effect=fail_generation_journal,
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)
            self.assertTrue(identity.snapshot_manifest_path.is_file())

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="MANIFEST_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            _terminal_after_completion(journal_path)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
            finally:
                connection.close()
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)

    def test_generation_published_replay_is_idempotent_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.CONSUMED,
            )

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            _terminal_after_completion(journal_path)
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
            finally:
                connection.close()
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.CONSUMED,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.GATE_B_DENIED",
            )

    def test_existing_canonical_generation_replay_keeps_generation(self) -> None:
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    1,
                )
            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(recovered.current_generation, 1)
            _terminal_after_completion(journal_path)
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            with recovered._operation_lease() as lease:
                connection = sqlite3.connect(
                    lease.stage.staged_db_path
                )
                try:
                    rows = connection.execute(
                        "SELECT source_raw, target_raw FROM tm_record "
                        "ORDER BY record_id"
                    ).fetchall()
                finally:
                    connection.close()
            self.assertEqual(len(rows), 3)
            self.assertIn(("same", "winner"), rows)
            self.assertIn(("other", "value"), rows)


class ActivationRecoveryPhaseTruthfulnessTests(unittest.TestCase):
    def test_advance_failure_before_manifest_journal_keeps_db_replaced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            journal_bytes = journal_path.read_bytes()

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._write_activation_journal_bytes",
                side_effect=OSError("injected journal write"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIsNone(first.current_generation)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["activation_status"], "ACTIVE")
            finally:
                connection.close()
            self.assertTrue(identity.snapshot_manifest_path.is_file())

            second = _fresh(identity)
            report = second.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="DB_REPLACED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.current_generation, 0)
            _terminal_after_completion(journal_path)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone(),
                    (1,),
                )
            finally:
                connection.close()

    def test_advance_failure_before_generation_journal_keeps_manifest_published(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            real_write = store_module._write_activation_journal_bytes
            calls = {"count": 0}

            def fail_second_write(descriptor: int, payload: bytes) -> None:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected generation journal write")
                real_write(descriptor, payload)

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._write_activation_journal_bytes",
                side_effect=fail_second_write,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIsNone(first.current_generation)
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)
            self.assertTrue(identity.snapshot_manifest_path.is_file())

            second = _fresh(identity)
            report = second.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="MANIFEST_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.current_generation, 0)
            _terminal_after_completion(journal_path)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
            finally:
                connection.close()

    def test_manifest_replace_failure_keeps_db_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_manifest",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            journal_bytes = journal_path.read_bytes()

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._replace_activation_file",
                side_effect=OSError("injected manifest replace"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_MANIFEST_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            self.assertTrue(
                journal._record.candidate_manifest_temp_path.is_file()
            )
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["activation_status"], "ACTIVE")
            finally:
                connection.close()

            second = _fresh(identity)
            report = _recovered_report(second)
            self.assertEqual(report.action, "COMPLETED")
            self.assertEqual(report.generation, 0)
            _terminal_after_completion(journal_path)
            self.assertTrue(identity.snapshot_manifest_path.is_file())

    def test_receipt_fsync_failure_keeps_db_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._fsync_activation_file",
                side_effect=OSError("injected receipt fsync"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_RECEIPT_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), DB_REPLACED)

            second = _fresh(identity)
            report = _recovered_report(second)
            self.assertEqual(report.action, "COMPLETED")
            self.assertEqual(report.generation, 0)
            _terminal_after_completion(journal_path)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
            finally:
                connection.close()

    def test_journal_replace_failure_keeps_truthful_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._validate_published_activation_set",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertTrue(identity.snapshot_manifest_path.is_file())

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store.os.replace",
                side_effect=OSError("injected journal replace"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_WRITE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), DB_REPLACED)
            self.assertFalse(
                _activation_journal_temp_path(journal_path).exists()
            )

            second = _fresh(identity)
            self.assertEqual(
                _recovered_report(second).action,
                "COMPLETED",
            )
            _terminal_after_completion(journal_path)

    def test_journal_dir_fsync_failure_after_manifest_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)

            def fail_manifest_fsync(path: Path) -> None:
                if _lstat_journal_phase(journal_path) is MANIFEST_PUBLISHED:
                    raise OSError("injected manifest journal fsync")

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._fsync_activation_directory",
                side_effect=fail_manifest_fsync,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)

            second = _fresh(identity)
            self.assertEqual(
                second.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="MANIFEST_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.current_generation, 0)
            _terminal_after_completion(journal_path)

    def test_journal_dir_fsync_failure_after_terminal_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)

            def fail_terminal_fsync(path: Path) -> None:
                if _lstat_journal_phase(journal_path) is GENERATION_PUBLISHED:
                    raise OSError("injected terminal journal fsync")

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._fsync_activation_directory",
                side_effect=fail_terminal_fsync,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.JOURNAL_DURABILITY_UNPROVEN",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIsNone(first.current_generation)
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

            second = _fresh(identity)
            report = second.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.current_generation, 0)
            _terminal_after_completion(journal_path)


class ActivationRecoveryTerminalProtocolTests(unittest.TestCase):
    def test_no_journal_discovery_rehydrates_completed_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered).action,
                "COMPLETED",
            )
            db_bytes = identity.canonical_sidecar_path.read_bytes()
            manifest_bytes = identity.snapshot_manifest_path.read_bytes()
            journal_path.unlink()
            self.assertFalse(journal_path.exists())

            discovered = _fresh(identity)
            self.assertIsNone(discovered.current_generation)
            report = discovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(discovered.state, "READY")
            self.assertEqual(discovered.current_generation, 0)
            with discovered._operation_lease() as lease:
                self.assertEqual(lease.generation, 0)
                self.assertEqual(
                    lease.stage.staged_db_path,
                    identity.canonical_sidecar_path,
                )
            self.assertEqual(
                identity.canonical_sidecar_path.read_bytes(),
                db_bytes,
            )
            self.assertEqual(
                identity.snapshot_manifest_path.read_bytes(),
                manifest_bytes,
            )
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
            finally:
                connection.close()

    def test_discovery_rejects_tampered_active_set(self) -> None:
        for tamper in ("manifest", "database"):
            with self.subTest(tamper=tamper):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        coordinator,
                        sealed,
                        prepared,
                        journal,
                    ) = _first_prepared(root)
                    journal_path = journal.journal_path
                    with patch(
                        "tm_sqlite_store._publish_activation_receipt",
                        side_effect=OSError("injected"),
                    ):
                        with self.assertRaises(OSError):
                            coordinator.publish_activation(prepared, journal)
                    recovered = _fresh(identity)
                    self.assertEqual(
                        _recovered_report(recovered).action,
                        "COMPLETED",
                    )
                    journal_path.unlink()
                    if tamper == "manifest":
                        identity.snapshot_manifest_path.write_text(
                            "tampered",
                            encoding="utf-8",
                        )
                    else:
                        identity.canonical_sidecar_path.write_bytes(
                            b"not a sqlite database"
                        )
                    discovered = _fresh(identity)
                    with self.assertRaises(ActivationPreparationError) as raised:
                        discovered.recover_durable_activation()
                    self.assertEqual(discovered.state, "ACTIVATING")
                    self.assertIsNone(discovered.current_generation)
                    self.assertIn(
                        raised.exception.code,
                        (
                            "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
                            "ACTIVATION.RECOVERY_DISCOVERY_FAILED",
                        ),
                    )

    def test_live_published_terminal_replayed_by_fresh_coordinators(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            first = _fresh(identity)
            self.assertEqual(
                first.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            second = _fresh(identity)
            self.assertEqual(
                second.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            _terminal_after_completion(journal_path)
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.CONSUMED,
            )

    def test_next_activation_supersedes_terminal_journal_and_cancel_restores(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            _terminal_after_completion(journal_path)
            sealed_two = _second_sealed(coordinator, identity, root)
            prepared_two = coordinator.activate(sealed_two)
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            journal_two = coordinator.publish_prepared_activation(
                prepared_two
            )
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertEqual(
                journal_two.journal_path,
                journal_path,
            )
            backup_paths = tuple(
                asset.backup_path for asset in prepared_two._backup_assets
            )
            for backup_path in backup_paths:
                self.assertTrue(backup_path.is_file())

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            self.assertFalse(journal_path.exists())
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            with recovered._operation_lease() as lease:
                self.assertEqual(
                    lease.stage.staged_db_path,
                    identity.canonical_sidecar_path,
                )
                self.assertEqual(lease.generation, 0)

            second_fresh = _fresh(identity)
            report = second_fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(second_fresh.current_generation, 0)
            with second_fresh._operation_lease() as lease:
                self.assertEqual(
                    lease.stage.staged_db_path,
                    identity.canonical_sidecar_path,
                )
                self.assertEqual(lease.generation, 0)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "0")
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tm_snapshot_binding"
                    ).fetchone(),
                    (1,),
                )
            finally:
                connection.close()
            third_fresh = _fresh(identity)
            self.assertEqual(
                third_fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(third_fresh.current_generation, 0)
            # exactly one prior generation survives, never a second one
            self.assertFalse(journal_path.exists())
            self.assertFalse(
                list(root.glob("*.localcat-recovery.*.bak"))
            )

    def test_terminal_journal_retirement_unlink_failure_fails_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            sealed_two = _second_sealed(coordinator, identity, root)
            prepared_two = coordinator.activate(sealed_two)
            real_unlink = os.unlink

            def fail_journal_unlink(path: Path) -> None:
                if Path(path) == journal_path:
                    raise OSError("injected journal unlink")
                real_unlink(path)

            with patch(
                "tm_sqlite_store.os.unlink",
                side_effect=fail_journal_unlink,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared_two)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_JOURNAL_RETIRE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            with self.assertRaises(ActivationPreparationError) as pending:
                coordinator.activate(sealed_two)
            self.assertEqual(
                pending.exception.code,
                "ACTIVATION.CONCURRENT_PREPARATION",
            )
            self.assertTrue(pending.exception.retryable)
            fresh = _fresh(identity)
            self.assertEqual(
                _recovered_report(fresh).action,
                "COMPLETED",
            )
            self.assertEqual(fresh.current_generation, 0)

    def test_terminal_publish_file_fsync_failure_preserves_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            sealed_two = _second_sealed(coordinator, identity, root)
            prepared_two = coordinator.activate(sealed_two)
            with patch(
                "tm_sqlite_store._fsync_activation_journal",
                side_effect=OSError("injected terminal file fsync"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared_two)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.TERMINAL_WRITE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            self.assertFalse(store_module._lstat_any_entry(terminal_path))
            self.assertFalse(
                store_module._lstat_any_entry(
                    store_module._activation_terminal_temp_path(
                        terminal_path
                    )
                )
            )
            fresh = _fresh(identity)
            report = fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)

    def test_terminal_publish_dir_fsync_failure_fails_stop_with_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            sealed_two = _second_sealed(coordinator, identity, root)
            prepared_two = coordinator.activate(sealed_two)

            def fail_terminal_dir_fsync(path: Path) -> None:
                if store_module._lstat_any_entry(terminal_path):
                    raise OSError("injected terminal dir fsync")

            with patch(
                "tm_sqlite_store._fsync_activation_directory",
                side_effect=fail_terminal_dir_fsync,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared_two)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.TERMINAL_DURABILITY_UNPROVEN",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            self.assertTrue(store_module._lstat_any_entry(terminal_path))
            fresh = _fresh(identity)
            report = fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)

    def test_prepared_durable_while_terminal_retire_fails_coexists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            sealed_two = _second_sealed(coordinator, identity, root)
            prepared_two = coordinator.activate(sealed_two)
            real_unlink = os.unlink

            def fail_terminal_unlink(path: Path) -> None:
                if Path(path) == terminal_path:
                    raise OSError("injected terminal unlink")
                real_unlink(path)

            with patch(
                "tm_sqlite_store.os.unlink",
                side_effect=fail_terminal_unlink,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared_two)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.TERMINAL_RETIRE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertTrue(store_module._lstat_any_entry(terminal_path))

            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            self.assertFalse(journal_path.exists())
            self.assertTrue(store_module._lstat_any_entry(terminal_path))
            second_fresh = _fresh(identity)
            self.assertEqual(
                second_fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(second_fresh.current_generation, 0)

    def test_crash_after_terminal_retirement_recovers_from_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            terminal_record = _parse_activation_journal_bytes(
                journal_path.read_bytes(),
                expected_journal_path=journal_path,
            )
            self.assertIs(terminal_record.phase, GENERATION_PUBLISHED)
            _ = coordinator._write_activation_terminal_locked(
                terminal_record
            )
            current_identity = (
                store_module._lstat_activation_journal_identity(journal_path)
            )
            self.assertIsNotNone(current_identity)
            assert current_identity is not None
            store_module._remove_owned_activation_journal_final(
                journal_path,
                current_identity,
            )
            self.assertFalse(journal_path.exists())
            self.assertTrue(terminal_path.is_file())

            first = _fresh(identity)
            self.assertEqual(
                first.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(first.current_generation, 0)
            second = _fresh(identity)
            self.assertEqual(
                second.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(second.current_generation, 0)

    def test_foreign_symlink_hardlink_terminal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            self.assertFalse(store_module._lstat_any_entry(terminal_path))

            terminal_path.write_text("not an activation terminal", encoding="utf-8")
            foreign_bytes = terminal_path.read_bytes()
            fresh = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(fresh.state, "ACTIVATING")
            self.assertEqual(terminal_path.read_bytes(), foreign_bytes)
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

            terminal_path.unlink()
            os.symlink(journal_path, terminal_path)
            fresh = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            )
            self.assertTrue(os.path.islink(terminal_path))

            terminal_path.unlink()
            os.link(identity.configured_jsonl_path, terminal_path)
            fresh = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                fresh.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_TERMINAL_INVALID",
            )
            self.assertNotEqual(
                os.lstat(terminal_path).st_nlink,
                1,
            )

            terminal_path.unlink()
            self.assertFalse(store_module._lstat_any_entry(terminal_path))
            fresh = _fresh(identity)
            report = fresh.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(fresh.current_generation, 0)

    def test_tampered_terminal_fails_closed_and_is_never_overwritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    coordinator.publish_activation(prepared, journal),
                    0,
                )
            terminal_path = store_module._activation_terminal_path(identity)
            terminal_record = _parse_activation_journal_bytes(
                journal_path.read_bytes(),
                expected_journal_path=journal_path,
            )

            with self.subTest(kind="wrong-terminal-kind"):
                terminal_path.write_text(
                    _serialize_activation_journal_record(
                        replace(
                            terminal_record,
                            phase=PREPARED,
                        )
                    ),
                    encoding="utf-8",
                )
                tampered_bytes = terminal_path.read_bytes()
                fresh = _fresh(identity)
                with self.assertRaises(ActivationPreparationError) as raised:
                    fresh.recover_durable_activation()
                self.assertEqual(
                    raised.exception.code,
                    "ACTIVATION.TERMINAL_COEXISTENCE_INVALID",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(terminal_path.read_bytes(), tampered_bytes)
                self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

            with self.subTest(kind="digest-tampered"):
                terminal_path.unlink()
                tampered_payload = bytearray(
                    _serialize_activation_journal_record(
                        terminal_record
                    ).encode("utf-8")
                )
                tampered_payload[-1] = ord("7") if tampered_payload[-1] != 55 else 0x6E
                terminal_path.write_bytes(bytes(tampered_payload))
                tampered_bytes = terminal_path.read_bytes()
                fresh = _fresh(identity)
                with self.assertRaises(ActivationPreparationError) as raised:
                    fresh.recover_durable_activation()
                self.assertEqual(
                    raised.exception.code,
                    "ACTIVATION.RECOVERY_TERMINAL_INVALID",
                )
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(terminal_path.read_bytes(), tampered_bytes)
                self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

            terminal_path.unlink()
            fresh = _fresh(identity)
            self.assertEqual(
                fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )

    def test_next_activation_after_cancelled_terminal_publishes_one_generation(
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
            prior_db_path = (root / ".prior.sqlite3").resolve()
            prior_bytes = prior_db_path.read_bytes()
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            terminal_path = store_module._activation_terminal_path(identity)
            self.assertFalse(store_module._lstat_any_entry(terminal_path))

            recovered = _fresh(identity)
            self.assertEqual(
                recovered.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            self.assertTrue(terminal_path.is_file())

            second_coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            self.assertEqual(
                second_coordinator.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(second_coordinator.current_generation, 0)
            sealed_two = _second_sealed(second_coordinator, identity, root)
            prepared_two = second_coordinator.activate(sealed_two)
            journal_two = second_coordinator.publish_prepared_activation(
                prepared_two
            )
            self.assertFalse(store_module._lstat_any_entry(terminal_path))
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                self.assertEqual(
                    second_coordinator.publish_activation(
                        prepared_two,
                        journal_two,
                    ),
                    1,
                )
            self.assertEqual(second_coordinator.current_generation, 1)
            self.assertIs(
                _registry(second_coordinator)
                ._token_entry(prepared_two._token)
                .state,
                contract_module.ActivationCapabilityState.CONSUMED,
            )
            self.assertIs(
                _registry(coordinator)._token_entry(prepared._token).state,
                contract_module.ActivationCapabilityState.TOKEN_ISSUED,
            )
            self.assertEqual(prior_db_path.read_bytes(), prior_bytes)

            fresh = _fresh(identity)
            self.assertEqual(
                fresh.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(fresh.current_generation, 1)
            connection = sqlite3.connect(identity.canonical_sidecar_path)
            try:
                self.assertEqual(_meta(connection)["generation"], "1")
                rows = connection.execute(
                    "SELECT source_raw, target_raw FROM tm_record "
                    "ORDER BY record_id"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                rows,
                [
                    ("same", "first"),
                    ("same", "winner"),
                    ("other", "value"),
                ],
            )
            prior_connection = sqlite3.connect(prior_db_path)
            try:
                prior_rows = prior_connection.execute(
                    "SELECT source_raw, target_raw FROM tm_record "
                    "ORDER BY record_id"
                ).fetchall()
            finally:
                prior_connection.close()
            self.assertEqual(prior_rows, [("prior", "canonical")])


class ActivationRecoveryBackupLifecycleTests(unittest.TestCase):
    def test_cancellation_partial_cleanup_resumes_idempotently(self) -> None:
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            self.assertEqual(len(backup_paths), 2)
            real_unlink = store_module._unlink_recovery_backup
            calls = {"count": 0}

            def fail_second_backup_unlink(path: Path) -> None:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected backup unlink")
                real_unlink(path)

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._unlink_recovery_backup",
                side_effect=fail_second_backup_unlink,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertTrue(journal_path.is_file())
            self.assertFalse(backup_paths[0].exists())
            self.assertTrue(backup_paths[1].is_file())

            second = _fresh(identity)
            report = second.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertEqual(second.state, "READY")
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertFalse(
                list(root.glob("*.localcat-recovery.*.bak"))
            )
            self.assertEqual(
                second.current_generation,
                0,
            )

    def test_completion_partial_cleanup_resumes_on_terminal_replay(
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._unlink_recovery_backup",
                side_effect=OSError("injected backup unlink"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            self.assertTrue(journal_path.is_file())
            for backup_path in backup_paths:
                self.assertTrue(backup_path.is_file())

            second = _fresh(identity)
            report = second.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=1,
                ),
            )
            self.assertEqual(second.current_generation, 1)
            _terminal_after_completion(journal_path)
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())
            self.assertFalse(
                list(root.glob("*.localcat-recovery.*.bak"))
            )

    def test_backup_cleanup_dir_fsync_failure_is_retryable(self) -> None:
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)

            first = _fresh(identity)
            with patch(
                "tm_sqlite_store._fsync_recovery_deletion_directory",
                side_effect=OSError("injected cleanup fsync"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

            second = _fresh(identity)
            self.assertEqual(
                _recovered_report(second).action,
                "COMPLETED",
            )
            for backup_path in backup_paths:
                self.assertFalse(backup_path.exists())

    def test_foreign_backup_file_preserves_journal_and_backups(self) -> None:
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            journal_bytes = journal_path.read_bytes()
            foreign = backup_paths[0]
            foreign.unlink()
            foreign.write_bytes(b"foreign replacement")
            foreign_bytes = foreign.read_bytes()

            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            for backup_path in backup_paths:
                self.assertTrue(backup_path.is_file())
            self.assertEqual(foreign.read_bytes(), foreign_bytes)
            self.assertFalse(identity.canonical_sidecar_path.exists())

    def test_hardlinked_backup_untouched(self) -> None:
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
            backup_paths = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            journal_bytes = journal_path.read_bytes()
            hardlink = root / "foreign-hardlink.bak"
            os.link(backup_paths[0], hardlink)
            self.assertEqual(os.lstat(backup_paths[0]).st_nlink, 2)

            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            for backup_path in backup_paths:
                self.assertTrue(backup_path.is_file())
            self.assertTrue(hardlink.is_file())
            self.assertEqual(os.lstat(hardlink).st_nlink, 2)
            self.assertFalse(identity.canonical_sidecar_path.exists())


class ActivationRecoveryFailStopTests(unittest.TestCase):
    def _assert_fail_stop(
        self,
        recovered: ResourceStoreCoordinator,
        journal_path: Path,
        expected_code: str,
        journal_bytes: bytes,
    ) -> None:
        with self.assertRaises(ActivationPreparationError) as raised:
            recovered.recover_durable_activation()
        self.assertEqual(raised.exception.code, expected_code)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(recovered.state, "ACTIVATING")
        self.assertIsNone(recovered.current_generation)
        self.assertEqual(journal_path.read_bytes(), journal_bytes)

    def test_journal_db_digest_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = _parse_activation_journal_bytes(
                journal_path.read_bytes(),
                expected_journal_path=journal_path,
            )
            journal_path.write_text(
                _serialize_activation_journal_record(
                    replace(record, stage_db_digest="0" * 64)
                ),
                encoding="utf-8",
            )
            tampered = journal_path.read_bytes()
            recovered = _fresh(identity)
            self._assert_fail_stop(
                recovered,
                journal_path,
                "ACTIVATION.RECOVERY_SEAL_EVIDENCE_INVALID",
                tampered,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())

    def test_manifest_tamper_rolls_back_before_receipt_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
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
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertTrue(
                store_module._activation_terminal_path(identity).is_file()
            )
            quarantine_dir = store_module._activation_quarantine_directory(
                identity,
                record,
            )
            self.assertTrue(quarantine_dir.is_dir())
            quarantined_db = (
                quarantine_dir / identity.canonical_sidecar_path.name
            )
            connection = sqlite3.connect(quarantined_db)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM tm_snapshot_receipt"
                    ).fetchall(),
                    [("issued",)],
                )
            finally:
                connection.close()

    def test_generation_published_journal_without_effects_rolls_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            _rewrite_phase(journal_path, GENERATION_PUBLISHED)
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
            self.assertFalse(journal_path.exists())
            self.assertTrue(
                store_module._activation_terminal_path(identity).is_file()
            )
            again = _fresh(identity)
            self.assertIsNone(again.rollback_durable_activation())
            self.assertEqual(
                again.recover_durable_activation(),
                None,
            )

    def test_candidate_reappeared_in_db_replaced_window_rolls_back(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            record = journal._record
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal_path), DB_REPLACED)
            candidate_path = record.candidate_stage_db_path
            candidate_path.write_bytes(b"reappeared candidate")
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
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(journal_path.exists())
            self.assertTrue(
                store_module._activation_terminal_path(identity).is_file()
            )
            quarantine_dir = store_module._activation_quarantine_directory(
                identity,
                record,
            )
            self.assertTrue(
                (
                    quarantine_dir / identity.canonical_sidecar_path.name
                ).is_file()
            )
            self.assertTrue(
                (
                    quarantine_dir / record.candidate_manifest_temp_path.name
                ).is_file()
            )
            self.assertEqual(candidate_path.read_bytes(), b"reappeared candidate")

    def test_source_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._publish_activation_receipt",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(OSError):
                    coordinator.publish_activation(prepared, journal)
            identity.configured_jsonl_path.write_bytes(
                SOURCE_BYTES + b'{"source":"x","target":"y"}\n'
            )
            recovered = _fresh(identity)
            self._assert_fail_stop(
                recovered,
                journal_path,
                "ACTIVATION.RECOVERY_ASSET_MUTATED",
                journal_path.read_bytes(),
            )

    def test_prior_missing_rolls_back_from_backups(self) -> None:
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
            prior_bytes = prior_db_path.read_bytes()
            prior_manifest_path = record.prior_manifest_path
            self.assertIsNotNone(prior_manifest_path)
            assert prior_manifest_path is not None
            prior_manifest_bytes = prior_manifest_path.read_bytes()
            prior_db_path.unlink()
            recovered = _fresh(identity)
            self.assertEqual(
                _recovered_report(recovered),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="ROLLED_BACK",
                    generation=0,
                ),
            )
            self.assertEqual(recovered.current_generation, 0)
            self.assertEqual(prior_db_path.read_bytes(), prior_bytes)
            self.assertEqual(
                prior_manifest_path.read_bytes(),
                prior_manifest_bytes,
            )
            self.assertFalse(journal_path.exists())
            self.assertTrue(
                store_module._activation_terminal_path(identity).is_file()
            )
            prior_db_backup_path = record.prior_db_backup_path
            prior_manifest_backup_path = record.prior_manifest_backup_path
            assert prior_db_backup_path is not None
            assert prior_manifest_backup_path is not None
            self.assertFalse(prior_db_backup_path.exists())
            self.assertFalse(prior_manifest_backup_path.exists())

    def test_hardlinked_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            other = root / "other-journal.json"
            other.write_bytes(journal_path.read_bytes())
            journal_path.unlink()
            os.link(other, journal_path)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_JOURNAL_INVALID",
            )
            self.assertEqual(recovered.state, "ACTIVATING")

    def test_journal_temp_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            temp_path = _activation_journal_temp_path(journal_path)
            journal_path.unlink()
            temp_path.write_text("leftover", encoding="utf-8")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_JOURNAL_TEMP_CONFLICT",
            )
            blocked = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as pending:
                blocked.activate(sealed)
            self.assertEqual(
                pending.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )

    def test_prepared_phase_with_replaced_database_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            journal_path = journal.journal_path
            with patch(
                "tm_sqlite_store._fsync_activation_directory",
                side_effect=OSError("injected"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_activation(prepared, journal)
            self.assertEqual(raised.exception.code, "ACTIVATION.DB_REPLACE_FAILED")
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
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                source_bytes,
            )
            self.assertFalse(journal_path.exists())
            self.assertTrue(
                store_module._activation_terminal_path(identity).is_file()
            )

    def test_recovery_requires_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_STATE_INVALID",
            )
            self.assertTrue(raised.exception.retryable)


class ActivationRecoveryGateTests(unittest.TestCase):
    def test_activate_blocked_while_journal_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = _first_prepared(
                root
            )
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, None)

    def test_no_journal_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            recovered = _fresh(identity)
            self.assertIsNone(recovered.recover_durable_activation())
            self.assertEqual(recovered.state, "READY")

    def test_no_premature_visibility_before_recovery(self) -> None:
        for fault_path in ("receipt", "manifest"):
            with self.subTest(fault_path=fault_path):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (
                        identity,
                        coordinator,
                        sealed,
                        prepared,
                        journal,
                    ) = _first_prepared(root)
                    target = (
                        "tm_sqlite_store._publish_activation_receipt"
                        if fault_path == "receipt"
                        else "tm_sqlite_store._publish_activation_manifest"
                    )
                    with patch(target, side_effect=OSError("injected")):
                        with self.assertRaises(OSError):
                            coordinator.publish_activation(prepared, journal)
                    recovered = _fresh(identity)
                    self.assertIsNone(recovered.current_generation)
                    with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                        with recovered._operation_lease():
                            pass
                    self.assertEqual(
                        raised.exception.code,
                        "STORE.CANONICAL_UNAVAILABLE",
                    )
                    report = recovered.recover_durable_activation()
                    self.assertIsNotNone(report)
                    assert report is not None
                    self.assertEqual(report.action, "COMPLETED")
                    self.assertEqual(recovered.current_generation, 0)

    def test_report_is_code_only(self) -> None:
        report = ActivationRecoveryReport(
            phase="GENERATION_PUBLISHED",
            action="COMPLETED",
            generation=0,
        )
        rendered = repr(report)
        self.assertNotIn("/", rendered)
        self.assertNotIn(".jsonl", rendered)
        self.assertNotIn(".sqlite3", rendered)
        self.assertNotIn("token", rendered.lower())
        for bad_action in ("CANCELLEDX", ""):
            with self.assertRaises((TypeError, ValueError)):
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action=bad_action,
                    generation=None,
                )
        rolled_back = ActivationRecoveryReport(
            phase="PREPARED",
            action="ROLLED_BACK",
            generation=None,
        )
        self.assertEqual(rolled_back.action, "ROLLED_BACK")
        self.assertNotIn("token", repr(rolled_back).lower())
        with self.assertRaises((TypeError, ValueError)):
            ActivationRecoveryReport(
                phase="PREPARED",
                action=cast(Any, None),
                generation=None,
            )
        with self.assertRaises((TypeError, ValueError)):
            ActivationRecoveryReport(
                phase="NOT_A_PHASE",
                action="CANCELLED",
                generation=None,
            )
        with self.assertRaises((TypeError, ValueError)):
            ActivationRecoveryReport(
                phase="PREPARED",
                action="CANCELLED",
                generation=-1,
            )


def _lstat_journal_phase(journal_path: Path) -> _ActivationJournalPhase | None:
    """Parse the journal only when the file is present (fsync seam helper)."""

    try:
        payload = journal_path.read_bytes()
    except FileNotFoundError:
        return None
    return _parse_activation_journal_bytes(
        payload,
        expected_journal_path=journal_path,
    ).phase


if __name__ == "__main__":
    unittest.main()
