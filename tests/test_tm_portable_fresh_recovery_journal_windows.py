"""Windows journal-only tests for the 5.8a fresh recovery snapshot."""

from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest

import tm_activation_journal
from tm_contracts import CanonicalResourceIdentity
from tm_migration import MigrationReport, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


_SOURCE_BYTES = b'{"source":"same","target":"winner"}\n'


def _fixture(
    root: Path,
) -> tuple[
    CanonicalResourceIdentity,
    ResourceStoreCoordinator,
    TMMigrationService,
]:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(_SOURCE_BYTES)
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


def _inspect(
    identity: CanonicalResourceIdentity,
    service: TMMigrationService,
) -> tm_activation_journal._PortableFreshRecoverySnapshot:
    with service._acquire_initial_reservation() as reservation:
        inputs = reservation.portable_recovery_inputs()
        return tm_activation_journal._WindowsPortableFreshRecoveryOwner.inspect(
            identity=identity,
            canonical_store_id="store.primary",
            backend=inputs["platform"],
            persistent_private=inputs["persistent_private"],
            descendant_inspection=inputs["descendant_inspection"],
            caller_borrow=inputs["caller_borrow"],
        )


def _remove_long_quarantine(root: Path) -> None:
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableFreshRecoveryJournalTests(unittest.TestCase):
    def test_absent_private_root_returns_no_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity, _coordinator, service = _fixture(root)

            snapshot = _inspect(identity, service)

            self.assertEqual(snapshot.state, "NO_FACTS")
            self.assertIsNone(snapshot.highest_phase)
            self.assertIsNone(snapshot.pending_record)
            self.assertIsNone(snapshot.cancelled_record)
            self.assertEqual(snapshot.phase_records, ())

    def test_complete_publication_reopens_as_one_verified_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport)

                snapshot = _inspect(identity, service)

                self.assertEqual(snapshot.state, "PENDING")
                self.assertEqual(snapshot.namespace_state, "JOURNAL")
                self.assertEqual(snapshot.highest_phase, "GENERATION_PUBLISHED")
                self.assertIsNotNone(snapshot.pending_record)
                self.assertIsNone(snapshot.cancelled_record)
                self.assertEqual(
                    tuple(record.unsigned.phase for record in snapshot.phase_records),
                    (
                        "DB_REPLACED",
                        "MANIFEST_PUBLISHED",
                        "GENERATION_PUBLISHED",
                    ),
                )
            finally:
                _remove_long_quarantine(root)

    def test_complete_generation_retires_exact_live_stage_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport)
                private_root = (
                    root
                    / tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                pending_record = (
                    tm_activation_journal._parse_portable_activation_journal_bytes(
                        (private_root / "activation-journal-v3.json").read_bytes()
                    )
                )
                unsigned = pending_record.unsigned
                quarantine = (
                    root
                    / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
                    / tm_activation_journal._portable_initial_stage_quarantine_name(
                        identity,
                        unsigned,
                    )
                )
                for name in (
                    unsigned.candidate_stage_db_name,
                    unsigned.candidate_manifest_temp_name,
                ):
                    os.rename(
                        "\\\\?\\" + str(quarantine / name),
                        "\\\\?\\" + str(root / name),
                    )

                with service._acquire_initial_reservation() as reservation:
                    inputs = reservation.portable_recovery_inputs()
                    snapshot = (
                        tm_activation_journal._WindowsPortableFreshRecoveryOwner.inspect(
                            identity=identity,
                            canonical_store_id="store.primary",
                            backend=inputs["platform"],
                            persistent_private=inputs["persistent_private"],
                            descendant_inspection=inputs["descendant_inspection"],
                            caller_borrow=inputs["caller_borrow"],
                        )
                    )
                    tm_activation_journal._WindowsPortableCompletedStageRetirementOwner.retire(
                        identity=identity,
                        backend=inputs["platform"],
                        persistent_private=inputs["persistent_private"],
                        descendant_inspection=inputs["descendant_inspection"],
                        existing_retirement=inputs["existing_retirement"],
                        caller_borrow=inputs["caller_borrow"],
                        snapshot=snapshot,
                    )

                pending = snapshot.pending_record
                self.assertIsNotNone(pending)
                unsigned = pending.unsigned
                quarantine = (
                    root
                    / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
                    / tm_activation_journal._portable_initial_stage_quarantine_name(
                        identity,
                        unsigned,
                    )
                )
                self.assertEqual(
                    sorted(path.name for path in quarantine.iterdir()),
                    sorted(
                        (
                            unsigned.candidate_stage_db_name,
                            unsigned.candidate_manifest_temp_name,
                        )
                    ),
                )
                self.assertFalse((root / unsigned.candidate_stage_db_name).exists())
                self.assertFalse(
                    (root / unsigned.candidate_manifest_temp_name).exists()
                )
            finally:
                _remove_long_quarantine(root)

    def test_complete_generation_rebinds_exact_retired_stage_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport)
                with service._acquire_initial_reservation() as reservation:
                    inputs = reservation.portable_recovery_inputs()
                    snapshot = (
                        tm_activation_journal._WindowsPortableFreshRecoveryOwner.inspect(
                            identity=identity,
                            canonical_store_id="store.primary",
                            backend=inputs["platform"],
                            persistent_private=inputs["persistent_private"],
                            descendant_inspection=inputs["descendant_inspection"],
                            caller_borrow=inputs["caller_borrow"],
                        )
                    )
                    tm_activation_journal._WindowsPortableCompletedStageRetirementOwner.retire(
                        identity=identity,
                        backend=inputs["platform"],
                        persistent_private=inputs["persistent_private"],
                        descendant_inspection=inputs["descendant_inspection"],
                        existing_retirement=inputs["existing_retirement"],
                        caller_borrow=inputs["caller_borrow"],
                        snapshot=snapshot,
                    )

                pending = snapshot.pending_record
                self.assertIsNotNone(pending)
                unsigned = pending.unsigned
                quarantine = (
                    root
                    / tm_activation_journal._PORTABLE_ACTIVATION_QUARANTINE_ROOT
                    / tm_activation_journal._portable_initial_stage_quarantine_name(
                        identity,
                        unsigned,
                    )
                )
                self.assertEqual(
                    sorted(path.name for path in quarantine.iterdir()),
                    sorted(
                        (
                            unsigned.candidate_stage_db_name,
                            unsigned.candidate_manifest_temp_name,
                        )
                    ),
                )
            finally:
                _remove_long_quarantine(root)

    def test_unknown_candidate_residue_fails_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport)
                private_root = (
                    root
                    / tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                residue = private_root / "foreign.candidate"
                residue.write_bytes(b"foreign-recovery-residue")
                before = residue.read_bytes()

                with self.assertRaises(
                    tm_activation_journal.ActivationPreparationError
                ) as caught:
                    _inspect(identity, service)

                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(residue.read_bytes(), before)
            finally:
                _remove_long_quarantine(root)


if __name__ == "__main__":
    unittest.main()
