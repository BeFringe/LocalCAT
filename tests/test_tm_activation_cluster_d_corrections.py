"""Cluster D correction regression tests (batch 1).

Each test pins one accepted native-review finding that the Task 5.5-5.9
suite did not exercise before the fix:

1. ``drain_for_transition`` is used by recovery/rollback: live operation
   leases block the transition, new leases are rejected while DRAINING,
   the journal is published only after the lease is released, and a drain
   timeout restores READY without any disk transition.
2. Single-link closure is enforced on every journal-managed source/prior/
   candidate capture seam (before capture, during the descriptor read, and
   at final revalidation) and fails closed.
3. A PREPARED cancellation retires the journal-proven candidate
   DB/manifest pair into the deterministic quarantine so a later
   deterministic migration retry succeeds; interrupted retirement resumes
   idempotently from the terminal authority; foreign candidates fail
   closed.
4. A supersession cleans the superseded GENERATION_PUBLISHED record's
   journal-proven backups only after the terminal mirror is durable, so a
   cleanup failure retains a record that authenticates the retry and no
   backup becomes ownerless.
5. The activated-lineage marker is write-once, revalidated, never cleared,
   never trusted alone, and survives repeated generations; tampered,
   hardlinked, or orphaned-temp states fail closed or resume.
"""

from __future__ import annotations

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

import tm_activation_journal as journal_module
import tm_sqlite_store as store_module
from tests.test_tm_activation_journal import (
    SOURCE_BYTES,
    _candidate,
    _existing_fixture,
    _first_prepared,
    _identity,
    _prior_stage,
    _registry,
)
from tests.test_tm_activation_recovery import (
    _fresh,
    _phase,
)
from tests.test_tm_activation_rollback import _manifest_published_window
from tm_contracts import ActivationCapabilityState
from tm_sqlite_store import (
    ActivationPreparationError,
    ActivationRecoveryReport,
    ResourceStoreCoordinator,
    SQLiteStoreLifecycleError,
    _ActivationJournalPhase,
    _ActivationPreparation,
    _activation_journal_path,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_quarantine_directory,
    _activation_terminal_path,
    _parse_activation_journal_bytes,
    initialize_stage_schema,
)

PREPARED = _ActivationJournalPhase.PREPARED
DB_REPLACED = _ActivationJournalPhase.DB_REPLACED
MANIFEST_PUBLISHED = _ActivationJournalPhase.MANIFEST_PUBLISHED
GENERATION_PUBLISHED = _ActivationJournalPhase.GENERATION_PUBLISHED


def _live_view_coordinator(
    identity: Any,
    root: Path,
    *,
    timeout_seconds: float = 5.0,
) -> ResourceStoreCoordinator:
    """One READY coordinator with a live prior view and no preparation.

    Recovery and rollback refuse a coordinator that still owns a live
    preparation, so the drain tests need a second coordinator that serves
    operation leases over the same prior authority while a pending
    journal sits on disk.
    """

    prior = _prior_stage(root, identity)
    with patch("tm_sqlite_store._probe_fts5", return_value=True):
        return ResourceStoreCoordinator(
            stage=prior,
            canonical_store_id="store.primary",
            drain_timeout_seconds=timeout_seconds,
        )


def _db_replaced_pending(root: Path) -> Any:
    """One pending first activation journal advanced to DB_REPLACED.

    Recovery of a PREPARED journal cancels (Task 5.8), so the marker
    publication crash windows must start at DB_REPLACED where recovery
    advances toward GENERATION_PUBLISHED.
    """

    identity, coordinator, sealed, prepared, journal = _first_prepared(root)
    journal_path = journal.journal_path
    with patch(
        "tm_activation_recovery._publish_activation_receipt",
        side_effect=OSError("injected"),
    ):
        try:
            coordinator.publish_activation(prepared, journal)
        except OSError:
            pass
        else:
            raise AssertionError("OSError not raised")
    assert _phase(journal_path) is DB_REPLACED
    return identity, journal_path


class ActivationDrainTransitionTests(unittest.TestCase):
    def _pending_existing_canonical(self, root: Path):
        identity, _store, coordinator, _stage, sealed = _existing_fixture(
            root,
            fts5_available=True,
        )
        prepared = coordinator.activate(sealed)
        journal = coordinator.publish_prepared_activation(prepared)
        return identity, coordinator, prepared, journal

    def test_recovery_drain_blocks_on_live_lease_and_rejects_new_leases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_canonical(root)
            )
            journal_path = journal.journal_path
            recovered = _live_view_coordinator(identity, root)
            entered = threading.Event()
            release = threading.Event()
            reports: list[Any] = []
            errors: list[BaseException] = []

            def hold() -> None:
                with recovered._operation_lease():
                    entered.set()
                    release.wait(timeout=5)

            def recover() -> None:
                try:
                    reports.append(recovered.recover_durable_activation())
                except BaseException as error:
                    errors.append(error)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=recover)
            worker.start()
            self.assertTrue(
                recovered.wait_for_state("DRAINING", timeout_seconds=1)
            )
            with self.assertRaises(SQLiteStoreLifecycleError) as raised:
                with recovered._operation_lease():
                    pass
            self.assertEqual(
                raised.exception.code,
                "STORE.RESOURCE_DRAINING",
            )
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertEqual(errors, [])
            self.assertEqual(reports, [])
            release.set()
            holder.join(timeout=5)
            worker.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertEqual(
                reports,
                [
                    ActivationRecoveryReport(
                        phase="PREPARED",
                        action="CANCELLED",
                        generation=0,
                    )
                ],
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            self.assertFalse(journal_path.exists())

    def test_rollback_drain_blocks_on_live_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_canonical(root)
            )
            journal_path = journal.journal_path
            prior_db_path = journal._record.prior_db_path
            assert prior_db_path is not None
            prior_bytes = prior_db_path.read_bytes()
            recovered = _live_view_coordinator(identity, root)
            entered = threading.Event()
            release = threading.Event()
            reports: list[Any] = []
            errors: list[BaseException] = []

            def hold() -> None:
                with recovered._operation_lease():
                    entered.set()
                    release.wait(timeout=5)

            def rollback() -> None:
                try:
                    reports.append(recovered.rollback_durable_activation())
                except BaseException as error:
                    errors.append(error)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=rollback)
            worker.start()
            self.assertTrue(
                recovered.wait_for_state("DRAINING", timeout_seconds=1)
            )
            self.assertEqual(reports, [])
            release.set()
            holder.join(timeout=5)
            worker.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertEqual(
                reports,
                [
                    ActivationRecoveryReport(
                        phase="PREPARED",
                        action="ROLLED_BACK",
                        generation=0,
                    )
                ],
            )
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(prior_db_path.read_bytes(), prior_bytes)

    def test_recovery_drain_timeout_restores_ready_without_transition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_canonical(root)
            )
            journal_path = journal.journal_path
            journal_bytes = journal_path.read_bytes()
            recovered = _live_view_coordinator(
                identity,
                root,
                timeout_seconds=0.15,
            )
            entered = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []

            def hold() -> None:
                with recovered._operation_lease():
                    entered.set()
                    release.wait(timeout=5)

            def recover() -> None:
                try:
                    recovered.recover_durable_activation()
                except BaseException as error:
                    errors.append(error)

            holder = threading.Thread(target=hold)
            holder.start()
            self.assertTrue(entered.wait(timeout=2))
            worker = threading.Thread(target=recover)
            worker.start()
            self.assertTrue(
                recovered.wait_for_state("DRAINING", timeout_seconds=1)
            )
            worker.join(timeout=5)
            self.assertEqual(len(errors), 1)
            drain_error = errors[0]
            assert isinstance(drain_error, ActivationPreparationError)
            self.assertEqual(drain_error.code, "ACTIVATION.DRAIN_TIMEOUT")
            self.assertTrue(drain_error.retryable)
            self.assertEqual(recovered.state, "READY")
            self.assertEqual(recovered.current_generation, 0)
            self.assertEqual(journal_path.read_bytes(), journal_bytes)
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(
                _activation_terminal_path(identity).exists()
            )
            release.set()
            holder.join(timeout=5)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            self.assertFalse(journal_path.exists())


class ActivationSingleLinkClosureTests(unittest.TestCase):
    def test_hardlinked_source_denies_first_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            peer = root / ".source-peer"
            os.link(identity.configured_jsonl_path, peer)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(coordinator.state, "READY")
            self.assertFalse(
                _activation_journal_path(identity).exists()
            )

    def test_hardlinked_prior_database_denies_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_path = prior_view.stage.staged_db_path
            peer = root / ".prior-peer"
            os.link(prior_path, peer)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertEqual(coordinator.state, "READY")

    def test_hardlink_added_during_backup_capture_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_path = prior_view.stage.staged_db_path
            peer = root / ".during-capture-peer"
            real_open = journal_module._open_recovery_backup
            injected = {"done": False}

            def link_then_open(path: Path) -> int:
                if not injected["done"]:
                    injected["done"] = True
                    os.link(prior_path, peer)
                return real_open(path)

            with patch(
                "tm_activation_journal._open_recovery_backup",
                side_effect=link_then_open,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertEqual(
                os.lstat(prior_path).st_nlink,
                2,
            )


class ActivationCandidateRetirementTests(unittest.TestCase):
    def test_cancelled_candidates_quarantined_then_deterministic_retry_succeeds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root)
            )
            record = journal._record
            candidate_db = record.candidate_stage_db_path
            candidate_manifest = record.candidate_manifest_temp_path
            self.assertTrue(candidate_db.is_file())
            self.assertTrue(candidate_manifest.is_file())
            recovered = _fresh(identity)
            report = recovered.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=None,
                ),
            )
            self.assertFalse(candidate_db.exists())
            self.assertFalse(candidate_manifest.exists())
            quarantine_dir = _activation_quarantine_directory(
                identity,
                record,
            )
            self.assertEqual(
                {entry.name for entry in quarantine_dir.iterdir()},
                {candidate_db.name, candidate_manifest.name},
            )
            # The same deterministic migration can now be rebuilt and
            # prepared again: the old stage files were retired, so the
            # retry never hits MIGRATION.STAGE_SEALED.
            retry_coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                retry_coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            prepared = retry_coordinator.activate(sealed)
            retry_coordinator.publish_prepared_activation(prepared)
            self.assertEqual(
                {entry.name for entry in quarantine_dir.iterdir()},
                {candidate_db.name, candidate_manifest.name},
            )

    def test_interrupted_candidate_retirement_resumes_idempotently(
        self,
    ) -> None:
        for seam in ("rename", "fsync"):
            with self.subTest(seam=seam):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    identity, _coordinator, _sealed, _prepared, journal = (
                        _first_prepared(root)
                    )
                    record = journal._record
                    journal_path = journal.journal_path
                    quarantine_dir = _activation_quarantine_directory(
                        identity,
                        record,
                    )
                    real_rename = journal_module._quarantine_candidate_rename
                    real_fsync = journal_module._fsync_activation_directory
                    state = {"count": 0, "failed": False}

                    if seam == "rename":

                        def fail_second_rename(source: Path, target: Path) -> None:
                            state["count"] += 1
                            if state["count"] == 2:
                                state["failed"] = True
                                raise OSError("injected quarantine rename")
                            real_rename(source, target)

                        injection = (
                            "tm_activation_journal._quarantine_candidate_rename",
                            fail_second_rename,
                        )
                    else:

                        def fail_first_quarantine_fsync(path: Path) -> None:
                            if (
                                not state["failed"]
                                and Path(path) == quarantine_dir
                            ):
                                state["failed"] = True
                                raise OSError("injected quarantine fsync")
                            real_fsync(path)

                        injection = (
                            "tm_activation_journal._fsync_activation_directory",
                            fail_first_quarantine_fsync,
                        )

                    first = _fresh(identity)
                    with patch(*injection):
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as raised:
                            first.recover_durable_activation()
                    self.assertEqual(
                        raised.exception.code,
                        "ACTIVATION.QUARANTINE_FAILED",
                    )
                    self.assertTrue(raised.exception.retryable)
                    self.assertFalse(journal_path.exists())
                    self.assertTrue(
                        _activation_terminal_path(identity).is_file()
                    )

                    second = _fresh(identity)
                    self.assertIsNone(
                        second.recover_durable_activation()
                    )
                    self.assertEqual(second.state, "READY")
                    terminal_bytes = _activation_terminal_path(
                        identity
                    ).read_bytes()
                    self.assertEqual(
                        {
                            entry.name
                            for entry in quarantine_dir.iterdir()
                        },
                        {
                            record.candidate_stage_db_path.name,
                            record.candidate_manifest_temp_path.name,
                        },
                    )

                    third = _fresh(identity)
                    self.assertIsNone(
                        third.recover_durable_activation()
                    )
                    self.assertEqual(third.state, "READY")
                    self.assertEqual(
                        _activation_terminal_path(identity).read_bytes(),
                        terminal_bytes,
                    )
                    self.assertEqual(
                        {
                            entry.name
                            for entry in quarantine_dir.iterdir()
                        },
                        {
                            record.candidate_stage_db_path.name,
                            record.candidate_manifest_temp_path.name,
                        },
                    )

    def test_foreign_candidate_at_stage_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root)
            )
            record = journal._record
            candidate_db = record.candidate_stage_db_path
            journal_path = journal.journal_path
            real_rename = journal_module._quarantine_candidate_rename
            state = {"count": 0}

            def fail_first_rename(source: Path, target: Path) -> None:
                state["count"] += 1
                if state["count"] == 1:
                    raise OSError("injected quarantine rename")
                real_rename(source, target)

            first = _fresh(identity)
            with patch(
                "tm_activation_journal._quarantine_candidate_rename",
                side_effect=fail_first_rename,
            ):
                with self.assertRaises(ActivationPreparationError):
                    first.recover_durable_activation()
            # Terminal is durable and the main journal is retired, so a
            # foreign replacement of the candidate stage path is the
            # crash window for the fail-closed retirement rule.
            self.assertFalse(journal_path.exists())
            candidate_db.unlink()
            candidate_db.write_bytes(b"foreign replacement")

            second = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                second.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.QUARANTINE_FOREIGN",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(candidate_db.read_bytes(), b"foreign replacement")
            self.assertTrue(candidate_db.is_file())


class ActivationSupersessionCleanupTests(unittest.TestCase):
    def _second_sealed(
        self,
        coordinator: ResourceStoreCoordinator,
        identity: Any,
        root: Path,
        *,
        expected_prior_generation: int,
        label: str = "second",
    ) -> Any:
        """Build one fresh sealed stage at distinct paths for a later
        generation (mirrors tests.test_tm_activation_recovery._second_sealed
        with an explicit prior generation)."""

        from tests.test_tm_activation_recovery import _migration_draft
        from tm_contracts import (
            SNAPSHOT_MANIFEST_VERSION,
            SnapshotKind,
            SnapshotManifest,
            SnapshotReceipt,
            contract_to_json,
            snapshot_receipt_digest,
        )
        from tm_stage_sealer import StageSealer

        stage = store_module.MutableStageRef(
            stage_id=f"stage.{label}",
            resource_identity=identity,
            staged_db_path=(
                root / f".localcat-{label}.sqlite3"
            ).resolve(),
            manifest_temp_path=(
                root / f".localcat-{label}.manifest.tmp"
            ).resolve(),
        )
        initialize_stage_schema(stage, canonical_store_id="store.primary")
        second_store = store_module.SQLiteTMStore(
            stage,
            canonical_store_id="store.primary",
        )
        source_digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
        second_store.append_batch(
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
        revision = second_store.canonical_revision()
        receipt = SnapshotReceipt(
            snapshot_id=f"snapshot.migration.{source_digest[:24]}",
            resource_id=identity.resource_id,
            canonical_store_id="store.primary",
            exported_revision=revision.head_revision,
            jsonl_digest=source_digest,
            record_count=revision.record_count,
        )
        second_store.register_issued_snapshot_receipt(
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
        stage.manifest_temp_path.write_bytes(
            contract_to_json(manifest).encode()
        )
        return StageSealer(
            registry=coordinator.sealed_registry,
            canonical_store_id="store.primary",
        ).seal(
            stage,
            expected_prior_generation=expected_prior_generation,
        )

    def test_supersession_cleans_superseded_backups_before_new_prepared(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prepared = coordinator.activate(sealed)
            old_backups = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            journal = coordinator.publish_prepared_activation(prepared)
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                generation = coordinator.publish_activation(prepared, journal)
            self.assertEqual(generation, 1)
            self.assertIs(_phase(journal.journal_path), GENERATION_PUBLISHED)
            for backup_path in old_backups:
                self.assertTrue(backup_path.is_file())
            marker_path = _activation_lineage_marker_path(identity)
            marker_bytes = marker_path.read_bytes()
            marker_inode = os.lstat(marker_path).st_ino

            sealed_two = self._second_sealed(
                coordinator,
                identity,
                root,
                expected_prior_generation=1,
            )
            prepared_two = coordinator.activate(sealed_two)
            new_backups = tuple(
                asset.backup_path for asset in prepared_two._backup_assets
            )
            journal_two = coordinator.publish_prepared_activation(
                prepared_two
            )
            self.assertIs(_phase(journal_two.journal_path), PREPARED)
            for backup_path in old_backups:
                self.assertFalse(backup_path.exists())
            for backup_path in new_backups:
                self.assertTrue(backup_path.is_file())
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(os.lstat(marker_path).st_ino, marker_inode)
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                generation = coordinator.publish_activation(
                    prepared_two,
                    journal_two,
                )
            self.assertEqual(generation, 2)
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(os.lstat(marker_path).st_ino, marker_inode)

    def test_supersession_cleanup_failure_retains_record_and_retry_completes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prepared = coordinator.activate(sealed)
            old_backups = tuple(
                asset.backup_path for asset in prepared._backup_assets
            )
            journal = coordinator.publish_prepared_activation(prepared)
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                coordinator.publish_activation(prepared, journal)
            self.assertIs(_phase(journal.journal_path), GENERATION_PUBLISHED)
            journal_bytes = journal.journal_path.read_bytes()

            sealed_two = self._second_sealed(
                coordinator,
                identity,
                root,
                expected_prior_generation=1,
            )
            prepared_two = coordinator.activate(sealed_two)
            with patch(
                "tm_activation_journal._unlink_recovery_backup",
                side_effect=OSError("injected backup cleanup"),
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    coordinator.publish_prepared_activation(prepared_two)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            # Both authorities are durable and the superseded backups are
            # still owned, so no backup became ownerless.
            self.assertEqual(
                journal.journal_path.read_bytes(),
                journal_bytes,
            )
            self.assertIs(
                _phase(journal.journal_path),
                GENERATION_PUBLISHED,
            )
            self.assertTrue(_activation_terminal_path(identity).is_file())
            for backup_path in old_backups:
                self.assertTrue(backup_path.is_file())

            # A fresh recovery replays the completed generation and
            # finishes the idempotent cleanup of the retained record.
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
            for backup_path in old_backups:
                self.assertFalse(backup_path.exists())

            # The retry supersession now publishes PREPARED cleanly with
            # no ownerless backups.
            sealed_three = self._second_sealed(
                recovered,
                identity,
                root,
                expected_prior_generation=1,
                label="third",
            )
            prepared_three = recovered.activate(sealed_three)
            journal_three = recovered.publish_prepared_activation(
                prepared_three
            )
            self.assertIs(_phase(journal_three.journal_path), PREPARED)
            for backup_path in old_backups:
                self.assertFalse(backup_path.exists())


class ActivationLineageMarkerTests(unittest.TestCase):
    def _completed_first_activation(self, root: Path) -> Any:
        identity, coordinator, sealed, prepared, journal = _first_prepared(
            root
        )
        with patch("tm_sqlite_store._probe_fts5", return_value=True):
            coordinator.publish_activation(prepared, journal)
        return identity, journal

    def test_marker_written_once_and_never_cleared_by_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prepared = coordinator.activate(sealed)
            journal = coordinator.publish_prepared_activation(prepared)
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                coordinator.publish_activation(prepared, journal)
            marker_path = _activation_lineage_marker_path(identity)
            marker_bytes = marker_path.read_bytes()
            marker_inode = os.lstat(marker_path).st_ino
            journal_module._read_activation_lineage_marker(
                marker_path,
                identity=identity,
            )
            # The write-once revalidation never rewrites the marker.
            journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            self.assertEqual(os.lstat(marker_path).st_ino, marker_inode)

    def test_marker_without_pair_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal = self._completed_first_activation(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            identity.canonical_sidecar_path.unlink()
            identity.snapshot_manifest_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            self.assertTrue(marker_path.is_file())
            discovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                discovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(discovered.state, "ACTIVATING")
            self.assertTrue(marker_path.is_file())

    def test_pair_without_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal = self._completed_first_activation(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            marker_path.unlink()
            discovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                discovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(discovered.state, "ACTIVATING")

    def test_tampered_marker_fails_closed_and_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal = self._completed_first_activation(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            tampered = marker_path.read_bytes().replace(
                b'"tm.primary"',
                b'"tm.forged"',
                1,
            )
            marker_path.write_bytes(tampered)
            discovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                discovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(discovered.state, "ACTIVATING")
            self.assertEqual(marker_path.read_bytes(), tampered)

    def test_hardlinked_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal = self._completed_first_activation(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            peer = root / ".marker-peer"
            os.link(marker_path, peer)
            discovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                discovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(os.lstat(marker_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)

    def test_marker_write_failure_after_generation_published_resumes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal_path = _db_replaced_pending(root)
            real_write = journal_module._write_activation_journal_bytes

            def fail_marker_write(descriptor: int, payload: bytes) -> None:
                if b'"lineage_version"' in payload:
                    raise OSError("injected marker write")
                real_write(descriptor, payload)

            first = _fresh(identity)
            with patch(
                "tm_activation_journal._write_activation_journal_bytes",
                side_effect=fail_marker_write,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(first.state, "ACTIVATING")
            # The marker is ensured only after the GENERATION_PUBLISHED
            # journal is durable and the active set is re-proven, so the
            # completed main journal remains the cold-recovery authority.
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)
            marker_path = _activation_lineage_marker_path(identity)
            self.assertFalse(marker_path.exists())
            self.assertFalse(
                marker_path.with_name(f"{marker_path.name}.tmp").exists()
            )

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
            self.assertEqual(second.state, "READY")
            self.assertTrue(marker_path.is_file())
            journal_module._read_activation_lineage_marker(
                marker_path,
                identity=identity,
            )

    def test_orphaned_marker_temp_is_cleaned_only_when_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal_path = _db_replaced_pending(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(
                journal_module._activation_lineage_marker_payload(identity)
            )
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
            self.assertTrue(marker_path.is_file())
            self.assertFalse(temp_path.exists())
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)

    def test_foreign_marker_temp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, journal_path = _db_replaced_pending(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            peer = root / ".temp-peer"
            peer.write_bytes(b"foreign")
            os.link(peer, temp_path)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)
            self.assertFalse(marker_path.exists())
            # The marker is ensured only after GENERATION_PUBLISHED is
            # durable, so the foreign temp fails closed with the completed
            # journal as the recoverable authority.
            self.assertIs(_phase(journal_path), GENERATION_PUBLISHED)


class ActivationLineageOrderingTests(unittest.TestCase):
    """Corrections A1/A2: marker schema and publish ordering.

    1. The marker binds only version + resource_id + target_identity +
       digest, so the exact same marker validates unchanged when a
       coordinator/store id changes.
    2. The marker is ensured only after the GENERATION_PUBLISHED journal is
       durable and the final active-set revalidation/token consume, so a
       failure/rollback before durable GENERATION_PUBLISHED leaves no
       marker and the legacy resource stays available.
    """

    def _completed_first_activation(self, root: Path) -> Any:
        identity, coordinator, sealed, prepared, journal = _first_prepared(
            root
        )
        with patch("tm_sqlite_store._probe_fts5", return_value=True):
            coordinator.publish_activation(prepared, journal)
        return identity, coordinator, journal

    def test_marker_does_not_bind_canonical_store_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, journal = self._completed_first_activation(
                root
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            marker_bytes = marker_path.read_bytes()
            payload = json.loads(marker_bytes.decode("utf-8"))
            self.assertEqual(
                set(payload),
                {
                    "lineage_version",
                    "resource_id",
                    "target_identity",
                    "record_digest",
                },
            )
            self.assertNotIn("canonical_store_id", payload)
            # A coordinator with a different store id must validate the
            # exact same marker unchanged: the marker binds only the stable
            # lineage facts, never the mutable canonical store id.
            journal_module._read_activation_lineage_marker(
                marker_path,
                identity=identity,
            )
            journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(marker_path.read_bytes(), marker_bytes)
            # Recovery on a renamed store id still fails only on the store
            # identity (the marker itself keeps validating).
            renamed = ResourceStoreCoordinator(
                canonical_store_id="store.renamed",
                resource_identity=identity,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                renamed.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_MISMATCH",
            )
            self.assertEqual(marker_path.read_bytes(), marker_bytes)

    def test_rollback_before_durable_generation_published_leaves_no_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, sealed, prepared, journal = (
                _first_prepared(root)
            )
            journal_path = journal.journal_path
            _manifest_published_window(coordinator, prepared, journal)
            self.assertIs(_phase(journal_path), MANIFEST_PUBLISHED)
            marker_path = _activation_lineage_marker_path(identity)
            self.assertFalse(marker_path.exists())
            self.assertFalse(
                marker_path.with_name(f"{marker_path.name}.tmp").exists()
            )
            report = coordinator.rollback_durable_activation()
            if report is None:
                self.fail("rollback returned no report")
            self.assertEqual(report.action, "ROLLED_BACK")
            self.assertIsNone(report.generation)
            # A failed first activation rollback must never leave a marker:
            # the resource genuinely never crossed physical activation.
            self.assertFalse(marker_path.exists())
            self.assertFalse(
                marker_path.with_name(f"{marker_path.name}.tmp").exists()
            )
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )
            self.assertFalse(identity.canonical_sidecar_path.exists())
            self.assertFalse(identity.snapshot_manifest_path.exists())
            fresh = _fresh(identity)
            self.assertIsNone(fresh.recover_durable_activation())
            self.assertIsNone(fresh.current_generation)
            self.assertEqual(
                identity.configured_jsonl_path.read_bytes(),
                SOURCE_BYTES,
            )


class ActivationCancellationLineageTests(unittest.TestCase):
    """Correction A3: PREPARED cancellation validates lineage consistency.

    A prior-canonical cancellation requires a valid marker for the stable
    identity; a first-activation cancellation requires marker final/temp
    absence.  Missing/tampered/hardlinked markers fail closed before the
    CANCELLED terminal is written.
    """

    def _pending_existing_prepared(self, root: Path) -> Any:
        identity, _store, coordinator, _stage, sealed = _existing_fixture(
            root,
            fts5_available=True,
        )
        prepared = coordinator.activate(sealed)
        journal = coordinator.publish_prepared_activation(prepared)
        return identity, coordinator, prepared, journal

    def test_existing_prior_cancellation_missing_marker_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            self.assertTrue(marker_path.is_file())
            marker_path.unlink()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())

    def test_existing_prior_cancellation_tampered_marker_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            tampered = marker_path.read_bytes().replace(
                b'"tm.primary"',
                b'"tm.forged"',
                1,
            )
            marker_path.write_bytes(tampered)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertEqual(marker_path.read_bytes(), tampered)

    def test_existing_prior_cancellation_hardlinked_marker_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            peer = root / ".cancellation-marker-peer"
            os.link(marker_path, peer)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertEqual(os.lstat(marker_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)

    def test_first_activation_cancellation_rejects_leftover_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            marker_path.write_bytes(
                journal_module._activation_lineage_marker_payload(identity)
            )
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertTrue(marker_path.is_file())

    def test_first_activation_cancellation_rejects_leftover_marker_temp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"leftover temp")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertEqual(temp_path.read_bytes(), b"leftover temp")

    def test_existing_prior_cancellation_foreign_regular_marker_temp_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"conflicting marker temp")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            # The unrelated deterministic-path temp is preserved: a valid
            # final with a non-paired temp is never a complete state.
            self.assertEqual(
                temp_path.read_bytes(),
                b"conflicting marker temp",
            )
            self.assertEqual(os.lstat(temp_path).st_nlink, 1)
            self.assertTrue(marker_path.is_file())

    def test_existing_prior_cancellation_symlink_marker_temp_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.symlink_to(root / ".marker-temp-target")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertTrue(temp_path.is_symlink())
            self.assertTrue(marker_path.is_file())

    def test_existing_prior_cancellation_hardlinked_marker_temp_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            peer = root / ".cancellation-temp-peer"
            peer.write_bytes(b"foreign peer")
            os.link(peer, temp_path)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertIs(_phase(journal_path), PREPARED)
            self.assertFalse(_activation_terminal_path(identity).exists())
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertTrue(marker_path.is_file())

    def test_existing_prior_cancellation_paired_handoff_marker_finishes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            os.link(marker_path, temp_path)
            self.assertEqual(os.lstat(marker_path).st_nlink, 2)
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
            self.assertEqual(recovered.state, "READY")
            # The exact paired two-link handoff is finished durably: the
            # temporary is unlinked and the single-link final revalidated.
            self.assertFalse(temp_path.exists())
            self.assertEqual(os.lstat(marker_path).st_nlink, 1)
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())

    def test_terminal_replay_conflicting_marker_temp_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _prepared, journal = (
                self._pending_existing_prepared(root)
            )
            journal_path = journal.journal_path
            first = _fresh(identity)
            self.assertEqual(
                first.recover_durable_activation(),
                ActivationRecoveryReport(
                    phase="PREPARED",
                    action="CANCELLED",
                    generation=0,
                ),
            )
            # A durable PREPARED terminal with no main journal is the
            # terminal-only replay authority; a conflicting temp beside
            # the valid marker final must fail closed, never be ignored.
            terminal_path = _activation_terminal_path(identity)
            self.assertTrue(terminal_path.is_file())
            self.assertFalse(journal_path.exists())
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"conflicting marker temp")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(
                temp_path.read_bytes(),
                b"conflicting marker temp",
            )
            self.assertTrue(terminal_path.is_file())
            self.assertTrue(marker_path.is_file())


class ActivationLiveViewDiscoveryTests(unittest.TestCase):
    """Correction A4: a live view never bypasses marker+pair revalidation."""

    def _live_completed(self, root: Path) -> Any:
        identity, coordinator, sealed, prepared, journal = _first_prepared(
            root
        )
        with patch("tm_sqlite_store._probe_fts5", return_value=True):
            coordinator.publish_activation(prepared, journal)
        return identity, coordinator, journal

    def test_live_view_authority_deletion_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, journal = self._live_completed(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            self.assertTrue(marker_path.is_file())
            self.assertIsNotNone(coordinator._view)
            identity.canonical_sidecar_path.unlink()
            identity.snapshot_manifest_path.unlink()
            marker_path.unlink()
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")

    def test_live_view_tampered_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, journal = self._live_completed(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            self.assertIsNotNone(coordinator._view)
            tampered = marker_path.read_bytes().replace(
                b'"tm.primary"',
                b'"tm.forged"',
                1,
            )
            marker_path.write_bytes(tampered)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(coordinator.state, "ACTIVATING")
            self.assertEqual(marker_path.read_bytes(), tampered)

    def test_live_view_intact_authority_revalidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, coordinator, journal = self._live_completed(root)
            journal_path = journal.journal_path
            journal_path.unlink()
            marker_path = _activation_lineage_marker_path(identity)
            self.assertTrue(marker_path.is_file())
            self.assertIsNotNone(coordinator._view)
            report = coordinator.recover_durable_activation()
            self.assertEqual(
                report,
                ActivationRecoveryReport(
                    phase="GENERATION_PUBLISHED",
                    action="COMPLETED",
                    generation=0,
                ),
            )
            self.assertEqual(coordinator.state, "READY")
            self.assertEqual(coordinator.current_generation, 0)

    def _journal_free_completed(self, root: Path) -> Any:
        identity, coordinator, journal = self._live_completed(root)
        journal.journal_path.unlink()
        self.assertFalse(_activation_terminal_path(identity).exists())
        return identity, coordinator

    def test_discovery_foreign_regular_marker_temp_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator = self._journal_free_completed(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"conflicting marker temp")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            # Discovery never ignores a non-paired temp beside a valid
            # final: the temp is preserved and no COMPLETED/READY report.
            self.assertEqual(
                temp_path.read_bytes(),
                b"conflicting marker temp",
            )
            self.assertEqual(os.lstat(temp_path).st_nlink, 1)
            self.assertTrue(marker_path.is_file())

    def test_discovery_symlink_marker_temp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator = self._journal_free_completed(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.symlink_to(root / ".marker-temp-target")
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertTrue(temp_path.is_symlink())
            self.assertTrue(marker_path.is_file())

    def test_discovery_hardlinked_marker_temp_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator = self._journal_free_completed(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            peer = root / ".discovery-temp-peer"
            peer.write_bytes(b"foreign peer")
            os.link(peer, temp_path)
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertTrue(marker_path.is_file())

    def test_discovery_paired_handoff_marker_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator = self._journal_free_completed(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            os.link(marker_path, temp_path)
            self.assertEqual(os.lstat(marker_path).st_nlink, 2)
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
            # The exact paired two-link handoff is finished durably.
            self.assertFalse(temp_path.exists())
            self.assertEqual(os.lstat(marker_path).st_nlink, 1)

    def test_discovery_leftover_marker_temp_without_pair_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator = self._journal_free_completed(root)
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"leftover temp")
            identity.canonical_sidecar_path.unlink()
            identity.snapshot_manifest_path.unlink()
            marker_path.unlink()
            recovered = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                recovered.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_ACTIVE_SET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(recovered.state, "ACTIVATING")
            # A marker-family temp with no pair is never silently treated
            # as the clean never-activated legacy state.
            self.assertEqual(temp_path.read_bytes(), b"leftover temp")


class ActivationExternalDeletionTests(unittest.TestCase):
    """Correction A5: candidate retirement absence must be quarantine-proven."""

    def test_externally_deleted_candidate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root)
            )
            record = journal._record
            candidate_db = record.candidate_stage_db_path
            journal_path = journal.journal_path
            real_rename = journal_module._quarantine_candidate_rename
            state = {"count": 0}

            def fail_first_rename(source: Path, target: Path) -> None:
                state["count"] += 1
                if state["count"] == 1:
                    raise OSError("injected quarantine rename")
                real_rename(source, target)

            first = _fresh(identity)
            with patch(
                "tm_activation_journal._quarantine_candidate_rename",
                side_effect=fail_first_rename,
            ):
                with self.assertRaises(ActivationPreparationError) as raised:
                    first.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.QUARANTINE_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertFalse(journal_path.exists())
            self.assertTrue(_activation_terminal_path(identity).is_file())
            self.assertTrue(candidate_db.is_file())
            # External deletion of the candidate before replay: the exact
            # inode is not in the deterministic quarantine directory, so
            # retirement cannot be proven and the replay fails closed
            # instead of accepting bare absence.
            candidate_db.unlink()
            second = _fresh(identity)
            with self.assertRaises(ActivationPreparationError) as raised:
                second.recover_durable_activation()
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.QUARANTINE_MISSING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(second.state, "ACTIVATING")
            self.assertFalse(candidate_db.exists())


class ActivationMarkerTempGateTests(unittest.TestCase):
    """Correction D: the activate gate never ignores a conflicting temp.

    With no main journal and no terminal, a new preparation proceeds only
    when the pair/marker/temp state is complete: a valid final is accepted
    only with no temp (or the finished paired handoff), any conflicting
    non-paired regular, symlink, or hardlinked temp fails closed in
    ``RECOVERY_PENDING`` and is never deleted or overwritten, and the true
    never-activated state (no pair, no final, no temp) stays unchanged.
    """

    def test_activate_gate_rejects_foreign_regular_marker_temp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"conflicting marker temp")
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertIsNone(coordinator._preparation)
            self.assertEqual(
                temp_path.read_bytes(),
                b"conflicting marker temp",
            )
            self.assertTrue(marker_path.is_file())

    def test_activate_gate_rejects_symlink_marker_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.symlink_to(root / ".marker-temp-target")
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertIsNone(coordinator._preparation)
            self.assertTrue(temp_path.is_symlink())
            self.assertTrue(marker_path.is_file())

    def test_activate_gate_rejects_hardlinked_marker_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            peer = root / ".gate-temp-peer"
            peer.write_bytes(b"foreign peer")
            os.link(peer, temp_path)
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertIsNone(coordinator._preparation)
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertTrue(marker_path.is_file())

    def test_activate_gate_rejects_leftover_temp_without_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            marker_path = _activation_lineage_marker_path(identity)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"leftover temp")
            marker_path.unlink()
            with self.assertRaises(ActivationPreparationError) as raised:
                coordinator.activate(sealed)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.RECOVERY_PENDING",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertIsNone(coordinator._preparation)
            self.assertEqual(temp_path.read_bytes(), b"leftover temp")


class ActivationMarkerAtomicPublishTests(unittest.TestCase):
    """Correction B: marker publication is atomic no-clobber.

    The exclusive deterministic temporary is fully written/fsynced, the
    final is published with a hard-link that fails if the final exists,
    the parent is fsynced, and the temporary is unlinked only while it is
    still the exact paired inode.  A crash replay accepts the two-link
    handoff only for the same inode with the deterministic payload; any
    other foreign final/temp is never removed or overwritten.
    """

    def _identity(self, root: Path) -> Any:
        identity = _identity(root)
        return identity, _activation_lineage_marker_path(identity)

    def test_foreign_symlink_final_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            marker_path.symlink_to(root / ".marker-target")
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertTrue(marker_path.is_symlink())

    def test_foreign_regular_final_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            marker_path.write_bytes(b"foreign final")
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(marker_path.read_bytes(), b"foreign final")

    def test_paired_handoff_replay_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            payload = journal_module._activation_lineage_marker_payload(
                identity
            )
            temp_path.write_bytes(payload)
            os.link(temp_path, marker_path)
            journal_module._ensure_activation_lineage_marker(identity)
            self.assertFalse(temp_path.exists())
            self.assertEqual(marker_path.read_bytes(), payload)
            self.assertEqual(os.lstat(marker_path).st_nlink, 1)

    def test_paired_handoff_wrong_bytes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"wrong handoff payload")
            os.link(temp_path, marker_path)
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(
                temp_path.read_bytes(),
                b"wrong handoff payload",
            )
            self.assertEqual(
                marker_path.read_bytes(),
                b"wrong handoff payload",
            )
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)

    def test_foreign_regular_temp_wrong_bytes_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            temp_path.write_bytes(b"foreign temp")
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(temp_path.read_bytes(), b"foreign temp")
            self.assertEqual(os.lstat(temp_path).st_nlink, 1)
            self.assertFalse(marker_path.exists())

    def test_hardlinked_temp_fails_closed_and_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")
            peer = root / ".temp-peer"
            peer.write_bytes(b"foreign")
            os.link(peer, temp_path)
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(os.lstat(temp_path).st_nlink, 2)
            self.assertFalse(marker_path.exists())

    def test_hardlinked_final_without_handoff_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            payload = journal_module._activation_lineage_marker_payload(
                identity
            )
            marker_path.write_bytes(payload)
            peer = root / ".marker-peer"
            os.link(marker_path, peer)
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._ensure_activation_lineage_marker(identity)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(os.lstat(marker_path).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)

    def test_link_race_foreign_final_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, marker_path = self._identity(root)
            temp_path = marker_path.with_name(f"{marker_path.name}.tmp")

            def foreign_final_wins(
                source: Path,
                destination: Path,
                _identity: Any,
            ) -> None:
                marker_path.write_bytes(b"foreign raced final")
                raise FileExistsError("marker final appeared concurrently")

            with patch(
                "tm_activation_journal._publish_activation_lineage_marker_link",
                side_effect=foreign_final_wins,
            ):
                with self.assertRaises(
                    ActivationPreparationError
                ) as raised:
                    journal_module._ensure_activation_lineage_marker(
                        identity
                    )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.LINEAGE_MARKER_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertFalse(temp_path.exists())
            self.assertEqual(
                marker_path.read_bytes(),
                b"foreign raced final",
            )


class ActivationSingleLinkReadFsyncTests(unittest.TestCase):
    """Correction C: close the single-link chain on read/fsync/cleanup."""

    def test_hardlink_added_after_descriptor_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _store, coordinator, _stage, _sealed = _existing_fixture(
                root,
                fts5_available=True,
            )
            prior_view = coordinator._view
            assert prior_view is not None
            prior_path = prior_view.stage.staged_db_path
            capture = journal_module._capture_activation_file(
                prior_path,
                asset_kind="DATABASE",
            )
            peer = root / ".read-peer"
            real_identity = journal_module._activation_file_identity

            def link_then_identity(path: Path):
                if path == prior_path:
                    os.link(prior_path, peer)
                return real_identity(path)

            with patch(
                "tm_activation_journal._activation_file_identity",
                side_effect=link_then_identity,
            ):
                with self.assertRaises(
                    ActivationPreparationError
                ) as raised:
                    journal_module._read_activation_file_bytes(capture)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertEqual(os.lstat(prior_path).st_nlink, 2)

    def test_hardlink_added_before_final_capture_revalidation_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            asset = root / ".capture-asset"
            asset.write_bytes(b"payload")
            peer = root / ".capture-peer"
            real_identity = journal_module._activation_file_identity

            def link_then_identity(path: Path):
                if path == asset:
                    os.link(asset, peer)
                return real_identity(path)

            with patch(
                "tm_activation_journal._activation_file_identity",
                side_effect=link_then_identity,
            ):
                with self.assertRaises(
                    ActivationPreparationError
                ) as raised:
                    journal_module._capture_activation_file(
                        asset,
                        asset_kind="DATABASE",
                    )
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.PRIOR_ASSET_INVALID",
            )
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(os.lstat(peer).st_nlink, 2)
            self.assertEqual(os.lstat(asset).st_nlink, 2)

    def test_fsync_activation_file_rejects_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = root / ".fsync-asset"
            asset.write_bytes(b"payload")
            asset_identity = journal_module._activation_file_identity(asset)
            peer = root / ".fsync-peer"
            os.link(asset, peer)
            with self.assertRaises(OSError):
                journal_module._fsync_activation_file(
                    asset,
                    asset_identity,
                )
            self.assertEqual(os.lstat(asset).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)

    def test_remove_recovery_path_rejects_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / ".recovery-backup"
            backup.write_bytes(b"payload")
            backup_identity = journal_module._activation_file_identity(
                backup
            )
            peer = root / ".recovery-backup-peer"
            os.link(backup, peer)
            owned = journal_module._OwnedRecoveryPath(
                path=backup,
                identity=backup_identity,
            )
            with self.assertRaises(ActivationPreparationError) as raised:
                journal_module._remove_recovery_path(owned)
            self.assertEqual(
                raised.exception.code,
                "ACTIVATION.CLEANUP_FAILED",
            )
            self.assertTrue(raised.exception.retryable)
            self.assertTrue(backup.exists())
            self.assertEqual(os.lstat(backup).st_nlink, 2)
            self.assertEqual(os.lstat(peer).st_nlink, 2)




if __name__ == "__main__":
    unittest.main()
