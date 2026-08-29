"""Windows owner-API checks for the C6S-A initial reservation seam."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tm_migration
import tm_stage_sealer
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    SnapshotReceipt,
)
from tm_migration import TMMigrationService
from tm_gate_b import GateBEvaluator
from tm_sqlite_store import ActivationPreparationError, ResourceStoreCoordinator


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

    def _portable_sealed_stage(
        self,
        service: TMMigrationService,
        coordinator: ResourceStoreCoordinator,
        identity: CanonicalResourceIdentity,
    ) -> tuple[object, object, object]:
        with mock.patch("tm_sqlite_store._probe_fts5", return_value=True):
            build = service.build_mutable_stage(identity.configured_jsonl_path)
        stage = build.mutable_stage
        if stage is None:
            raise AssertionError("expected one mutable stage")
        reservation = service._acquire_initial_reservation()
        inputs = reservation.stage_seal_inputs()
        with mock.patch("tm_sqlite_store._probe_fts5", return_value=True):
            sealed = coordinator._seal_stage(
                stage,
                canonical_store_id="store.primary",
                expected_prior_generation=None,
                **inputs,
            )
        return reservation, sealed, coordinator._sealed_registry

    def test_real_reservation_spans_seal_two_gate_b_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=coordinator,
            )
            reservation, sealed, registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            try:
                entry = registry._entries[sealed.artifact.artifact_id]
                self.assertIs(
                    type(entry),
                    tm_stage_sealer._PortableRegistryEntry,
                )
                live_authority = entry.live_authority
                self.assertIsNotNone(live_authority)
                binding_calls: list[tuple[object, str, bytes]] = []
                lease_type = type(reservation._lease)
                real_reprove_binding = lease_type.reprove_binding

                def record_binding(
                    lease: object,
                    parent: object,
                    name: str,
                    payload: bytes,
                ) -> None:
                    binding_calls.append((parent, name, payload))
                    real_reprove_binding(lease, parent, name, payload)

                with mock.patch.object(
                    lease_type,
                    "reprove_binding",
                    new=record_binding,
                ):
                    for _pass in range(2):
                        report = GateBEvaluator(
                            registry=registry._readiness_view()
                        ).evaluate(sealed)
                        self.assertTrue(report.granted)
                        self.assertIsNotNone(report.grant)
                        reservation.reprove()
                    token = registry.issue_token(
                        sealed,
                        current_generation=None,
                    )
                    registry.cancel(token)
                self.assertGreaterEqual(len(binding_calls), 4)
                self.assertTrue(
                    all(
                        parent is reservation._root
                        and name == reservation._lock_name
                        and payload
                        == tm_migration._InitialActivationResourceReservation._payload(
                            identity
                        )
                        for parent, name, payload in binding_calls
                    )
                )
                self.assertTrue(live_authority.closed)
                self.assertFalse(reservation._root.closed)
                self.assertFalse(reservation._lease.closed)
                reservation.reprove()
            finally:
                reservation.release()

    def test_lock_tamper_denies_gate_b_without_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=coordinator,
            )
            reservation, sealed, registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            try:
                lock_path = root / (
                    f".{identity.canonical_sidecar_path.name}."
                    "localcat-initial-activation.lock"
                )
                lock_path.write_bytes(b"tampered-lock-payload")
                report = GateBEvaluator(
                    registry=registry._readiness_view()
                ).evaluate(sealed)
                self.assertFalse(report.granted)
                self.assertIsNone(report.grant)
                self.assertEqual(
                    report.error_code,
                    "GATE_B.ATTESTATION_UNAVAILABLE",
                )
                registry.retire_unissued_portable(sealed)
            finally:
                try:
                    reservation.release()
                except tm_migration._InitialActivationReservationError:
                    pass

    def test_first_gate_b_denial_retires_portable_entry_before_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=coordinator,
            )
            reservation, sealed, registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            entry = registry._entries[sealed.artifact.artifact_id]
            live_authority = entry.live_authority
            try:
                with mock.patch.object(
                    tm_stage_sealer._PortableAuthorityBorrow,
                    "reprove",
                    side_effect=tm_stage_sealer.StageSealError(
                        "SEALER.ARTIFACT_MUTATED"
                    ),
                ):
                    with self.assertRaisesRegex(
                        ActivationPreparationError,
                        "^ACTIVATION.GATE_B_DENIED$",
                    ):
                        coordinator.activate(sealed)
                self.assertEqual(registry._entries, {})
                self.assertEqual(registry._sealed_paths, {})
                self.assertEqual(registry._portable_borrows, {})
                self.assertTrue(live_authority.closed)
                self.assertFalse(reservation._root.closed)
                self.assertFalse(reservation._lease.closed)
                reservation.reprove()
            finally:
                reservation.release()

    def test_unreserved_import_rebuild_and_schema_seal_before_marker(self) -> None:
        for operation in ("import", "rebuild", "schema"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity = _identity(root)
                    coordinator = ResourceStoreCoordinator(
                        canonical_store_id="store.primary",
                        resource_identity=identity,
                    )
                    service = TMMigrationService(
                        resource_identity=identity,
                        canonical_store_id="store.primary",
                        coordinator=coordinator,
                    )
                    with mock.patch(
                        "tm_sqlite_store._probe_fts5",
                        return_value=True,
                    ):
                        build = service.build_mutable_stage(
                            identity.configured_jsonl_path
                        )
                    stage = build.mutable_stage
                    if stage is None:
                        raise AssertionError("expected one mutable stage")
                    with self.assertRaisesRegex(
                        tm_stage_sealer.StageSealError,
                        "^SEALER.ATTESTATION_UNAVAILABLE$",
                    ):
                        coordinator._seal_stage(
                            stage,
                            canonical_store_id="store.primary",
                            expected_prior_generation=(
                                0 if operation != "import" else None
                            ),
                            schema_upgrade=(operation == "schema"),
                        )
                    connection = sqlite3.connect(
                        str(stage.staged_db_path)
                    )
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT value FROM tm_meta "
                                "WHERE key = 'activation_status'"
                            ).fetchall(),
                            [("UNPUBLISHED",)],
                        )
                    finally:
                        connection.close()
                    self.assertEqual(coordinator._sealed_registry._entries, {})
                    self.assertEqual(
                        coordinator._sealed_registry._reservations,
                        {},
                    )

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
