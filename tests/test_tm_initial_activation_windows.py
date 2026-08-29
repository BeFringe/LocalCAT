"""Windows owner-API checks for the C6S-A initial reservation seam."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tm_migration
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    SnapshotReceipt,
)
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


SOURCE_BYTES = (
    b'{"source":"same","target":"first"}\n'
    b'{"source":"other","target":"value"}\n'
)


def _identity(root: Path) -> CanonicalResourceIdentity:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(SOURCE_BYTES)
    return CanonicalResourceIdentity.from_configured_jsonl(
        "tm.primary",
        source,
    )


def _service(
    identity: CanonicalResourceIdentity,
) -> TMMigrationService:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    return TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )


def _success_report(
    identity: CanonicalResourceIdentity,
    digest: str,
) -> MigrationReport:
    return MigrationReport(
        resource_id=identity.resource_id,
        canonical_store_id="store.primary",
        source_digest=digest,
        snapshot_receipt=SnapshotReceipt(
            snapshot_id="snapshot.migration.windows-test",
            resource_id=identity.resource_id,
            canonical_store_id="store.primary",
            exported_revision=0,
            jsonl_digest=digest,
            record_count=2,
        ),
        migrated_count=2,
        variant_count=2,
        skipped_count=0,
        diagnostics=(),
        activated_generation=0,
        canonical_exact_available=True,
        context_available=True,
        fuzzy_available=True,
    )


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsInitialActivationReservationSeamTests(unittest.TestCase):
    """Reservation seam evidence; full activation remains a later blocker."""

    def test_owner_activation_reservation_seam_returns_exact_report(self) -> None:
        """The seam is reached; the downstream baseline is not masked here."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            service = _service(identity)
            digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
            expected = _success_report(identity, digest)

            def complete(*, reservation: object, **_: object) -> MigrationReport:
                reservation.reprove()  # type: ignore[attr-defined]
                return expected

            with mock.patch.object(
                service,
                "_activate_initial_with_resource_reservation",
                side_effect=complete,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(outcome), MigrationReport)
            self.assertEqual(outcome, expected)
            self.assertNotEqual(
                getattr(outcome, "error_code", None),
                "MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE",
            )

    def test_owner_api_competition_and_release_without_repo_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            first = _service(identity)
            second = _service(identity)
            digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
            expected = _success_report(identity, digest)
            entered = threading.Event()
            release = threading.Event()
            outcomes: list[MigrationReport | BaseException] = []

            def complete(*, reservation: object, **_: object) -> MigrationReport:
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError("owner activation was not released")
                reservation.reprove()  # type: ignore[attr-defined]
                return expected

            def activate(owner: TMMigrationService) -> None:
                try:
                    outcomes.append(
                        owner.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    )
                except BaseException as error:
                    outcomes.append(error)

            previous_cwd = Path.cwd()
            try:
                os.chdir(tempfile.gettempdir())
                with mock.patch.object(
                    first,
                    "_activate_initial_with_resource_reservation",
                    side_effect=complete,
                ):
                    first_thread = threading.Thread(target=activate, args=(first,))
                    first_thread.start()
                    self.assertTrue(entered.wait(timeout=10))
                    second_outcome = second.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                    release.set()
                    first_thread.join(timeout=20)
                self.assertFalse(first_thread.is_alive())
                self.assertIs(type(second_outcome), tm_migration.MigrationFailure)
                self.assertEqual(
                    second_outcome.error_code,
                    "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
                )
                self.assertFalse(second_outcome.canonical_authority_published)
                self.assertTrue(second_outcome.canonical_authority_ambiguous)
                self.assertNotEqual(
                    second_outcome.error_code,
                    "MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE",
                )
                self.assertEqual(len(outcomes), 1)
                self.assertIs(type(outcomes[0]), MigrationReport)
                self.assertEqual(outcomes[0], expected)
                third = _service(identity)
                with mock.patch.object(
                    third,
                    "_activate_initial_with_resource_reservation",
                    side_effect=complete,
                ):
                    third_outcome = third.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(third_outcome), MigrationReport)
                self.assertEqual(third_outcome, expected)
            finally:
                os.chdir(previous_cwd)

    def test_owner_api_rejects_tampered_lock_payload_without_platform_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            service = _service(identity)
            digest = hashlib.sha256(SOURCE_BYTES).hexdigest()
            body_continued: list[bool] = []
            lock_path = root / (
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )

            def tamper(*, reservation: object, **_: object) -> MigrationReport:
                lock_path.write_bytes(b"tampered-lock-payload")
                try:
                    reservation.reprove()  # type: ignore[attr-defined]
                except tm_migration._InitialActivationReservationError:
                    raise
                body_continued.append(True)
                return _success_report(identity, digest)

            with mock.patch.object(
                service,
                "_activate_initial_with_resource_reservation",
                side_effect=tamper,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertFalse(outcome.canonical_authority_published)
            self.assertTrue(outcome.canonical_authority_ambiguous)
            self.assertEqual(body_continued, [])

    def test_production_entry_reproves_tampered_real_reservation_before_business(self) -> None:
        """A post-acquire payload tamper stops before recovery/business entry."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            service = _service(identity)
            lock_path = root / (
                f".{identity.canonical_sidecar_path.name}."
                "localcat-initial-activation.lock"
            )
            real_acquire = service._acquire_initial_reservation

            def acquire_and_tamper() -> object:
                reservation = real_acquire()
                self.assertIsNotNone(getattr(reservation, "_root", None))
                self.assertIsNotNone(getattr(reservation, "_lease", None))
                lock_path.write_bytes(b"tampered-after-acquire")
                return reservation

            with (
                mock.patch.object(
                    service,
                    "_acquire_initial_reservation",
                    side_effect=acquire_and_tamper,
                ),
                mock.patch.object(
                    service,
                    "_recover_existing_initial_activation",
                    wraps=service._recover_existing_initial_activation,
                ) as recover_existing,
                mock.patch.object(
                    service,
                    "_activate_initial_authority_transaction",
                    wraps=service._activate_initial_authority_transaction,
                ) as activate_transaction,
            ):
                try:
                    outcome = service.activate_initial(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                except tm_migration.PlatformFileError as error:
                    self.fail(f"platform error leaked through owner API: {error}")

            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertFalse(outcome.canonical_authority_published)
            self.assertTrue(outcome.canonical_authority_ambiguous)
            recover_existing.assert_not_called()
            activate_transaction.assert_not_called()
