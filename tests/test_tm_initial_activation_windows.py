"""Windows owner-API checks for the C6S-A initial reservation seam."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
from pathlib import PurePath
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tm_migration
import tm_activation_journal
import tm_sqlite_store
import tm_stage_sealer
import platform_fs_windows
from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateProofContext,
    PrivateProofObjectRole,
)
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


def _remove_long_quarantine(root: Path) -> None:
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


def _release_portable_test_authorities(
    coordinator: ResourceStoreCoordinator,
    preparation: object | None,
    reservation: object,
) -> None:
    """Test-only release; never projects cancellation as product recovery."""

    try:
        if preparation is not None:
            coordinator._sealed_registry.cancel(preparation._token)
    finally:
        reservation.release()


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

    def test_real_private_owner_publishes_only_portable_prepared(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                handle = coordinator.publish_portable_prepared_activation(
                    preparation,
                    **owner_inputs,
                )

                self.assertEqual(coordinator.state, "ACTIVATING")
                self.assertIsNone(coordinator.current_generation)
                self.assertIsNone(coordinator.active_store_path)
                self.assertFalse(identity.canonical_sidecar_path.exists())
                private_root = root / handle.private_directory_name
                key_path = private_root / "device.key"
                journal_path = private_root / handle.journal_name
                self.assertEqual(len(key_path.read_bytes()), 32)
                journal_bytes = journal_path.read_bytes()
                disk_record = (
                    tm_activation_journal._parse_portable_activation_journal_bytes(
                        journal_bytes
                    )
                )
                self.assertEqual(disk_record, handle._record)
                self.assertEqual(disk_record.unsigned.phase, "PREPARED")
                self.assertFalse(
                    (private_root / "activation-terminal-v3.json").exists()
                )
                self.assertEqual(
                    sorted(path.name for path in private_root.iterdir()),
                    ["activation-journal-v3.json", "device.key"],
                )
                platform = owner_inputs["platform"]
                persistent_private = owner_inputs["persistent_private"]
                caller_borrow = owner_inputs["caller_borrow"]
                caller_borrow.reprove(platform, identity)
                private_parent = platform.bind_parent(
                    reservation._root,
                    PurePath(
                        handle.private_directory_name,
                        "private-proof-placeholder",
                    ),
                )
                private_evidence = platform.prove_private(private_parent)
                key_file = platform.open_regular(
                    reservation._root,
                    PurePath(handle.private_directory_name, "device.key"),
                )
                secret = persistent_private.bind_device_secret(key_file)
                try:
                    tampered_proof = replace(
                        disk_record.private_directory_proof,
                        device_secret_mac=b"x" * 32,
                    )
                    tampered_record = (
                        tm_activation_journal._create_portable_activation_journal_record(
                            disk_record.unsigned,
                            tampered_proof,
                        )
                    )
                    reparsed = (
                        tm_activation_journal._parse_portable_activation_journal_bytes(
                            tm_activation_journal._serialize_portable_activation_journal_record(
                                tampered_record
                            )
                        )
                    )
                    self.assertEqual(reparsed, tampered_record)
                    context = PrivateProofContext(
                        PrivateProofObjectRole.PRIVATE_DIRECTORY,
                        tm_activation_journal._portable_activation_owner_context_sha256(
                            disk_record.unsigned
                        ),
                    )
                    with self.assertRaises(PlatformFileError) as caught:
                        persistent_private.verify(
                            private_evidence,
                            secret,
                            reparsed.private_directory_proof,
                            context,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
                    )
                finally:
                    secret.close()
                    key_file.close()
                    private_evidence.close()
                    private_parent.close()
                self.assertEqual(
                    coordinator._sealed_registry._portable_borrows,
                    {},
                )
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_existing_private_namespace_requires_fresh_recovery(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                owner_parent = owner_inputs["platform"].bind_parent(
                    reservation._root,
                    PurePath(private_name),
                )
                private = owner_inputs["platform"].create_private_directory(
                    owner_parent,
                    private_name,
                )
                private.close()
                owner_parent.close()
                (root / private_name / "foreign.bin").write_bytes(b"foreign")

                with self.assertRaises(ActivationPreparationError) as caught:
                    coordinator.publish_portable_prepared_activation(
                        preparation,
                        **owner_inputs,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertEqual(
                    sorted(path.name for path in (root / private_name).iterdir()),
                    ["foreign.bin"],
                )
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_w2_narrowing_failure_is_not_reported_as_lock_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            service = _service(identity)
            reservation = service._acquire_initial_reservation()
            try:
                with mock.patch(
                    "tm_migration.narrow_windows_persistent_private_proof",
                    side_effect=PlatformFileError(
                        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                        retryable=False,
                    ),
                ):
                    with self.assertRaises(ActivationPreparationError) as caught:
                        reservation.portable_journal_inputs()
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.PRIVATE_STORAGE_UNPROVEN",
                )
                self.assertFalse(
                    any(
                        path.name.startswith(".localcat-activation-private-v1.")
                        for path in root.iterdir()
                    )
                )
                reservation.reprove()
            finally:
                reservation.release()

    def test_pre_arm_authority_failure_leaves_no_private_namespace(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                borrow_type = type(preparation._physical_snapshot.live_reproof)
                with mock.patch.object(
                    borrow_type,
                    "reprove",
                    side_effect=tm_stage_sealer.StageSealError(
                        "SEALER.ARTIFACT_MUTATED"
                    ),
                ):
                    with self.assertRaises(ActivationPreparationError) as caught:
                        coordinator.publish_portable_prepared_activation(
                            preparation,
                            **owner_inputs,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.JOURNAL_ASSET_MUTATED",
                )
                self.assertFalse((root / private_name).exists())
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_pre_arm_programmer_error_passes_through_unchanged(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                borrow_type = type(preparation._physical_snapshot.live_reproof)
                programmer_error = TypeError("test programmer boundary")
                with mock.patch.object(
                    borrow_type,
                    "reprove",
                    side_effect=programmer_error,
                ):
                    with self.assertRaises(TypeError) as caught:
                        coordinator.publish_portable_prepared_activation(
                            preparation,
                            **owner_inputs,
                        )
                self.assertIs(caught.exception, programmer_error)
                self.assertFalse((root / private_name).exists())
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_key_and_journal_arm_uncertainty_preserve_exact_residue(self) -> None:
        for target_publish, expected_names in (
            (1, ["device.key"]),
            (2, ["activation-journal-v3.json", "device.key"]),
        ):
            with self.subTest(target_publish=target_publish):
                enabled = False
                publish_count = 0

                def fault(phase: str) -> None:
                    nonlocal publish_count
                    if enabled and phase == "publish_after_rename":
                        publish_count += 1
                        if publish_count == target_publish:
                            raise OSError("test publication uncertainty")

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity = _identity(root)
                    backend = platform_fs_windows.WindowsPlatformAdapter(
                        _fault_injector=fault
                    )
                    coordinator = ResourceStoreCoordinator(
                        canonical_store_id="store.primary",
                        resource_identity=identity,
                    )
                    service = TMMigrationService(
                        resource_identity=identity,
                        canonical_store_id="store.primary",
                        coordinator=coordinator,
                        platform_backend=backend,
                    )
                    reservation, sealed, _registry = self._portable_sealed_stage(
                        service,
                        coordinator,
                        identity,
                    )
                    preparation = None
                    try:
                        preparation = coordinator.activate(sealed)
                        owner_inputs = reservation.portable_journal_inputs()
                        private_name = (
                            tm_activation_journal._portable_activation_private_directory_name(
                                identity
                            )
                        )
                        enabled = True
                        with self.assertRaises(
                            ActivationPreparationError
                        ) as caught:
                            coordinator.publish_portable_prepared_activation(
                                preparation,
                                **owner_inputs,
                            )
                        enabled = False
                        self.assertEqual(
                            caught.exception.code,
                            "ACTIVATION.RECOVERY_REQUIRED",
                        )
                        self.assertEqual(
                            sorted(
                                path.name
                                for path in (root / private_name).iterdir()
                            ),
                            expected_names,
                        )
                        reservation.reprove()
                    finally:
                        enabled = False
                        _release_portable_test_authorities(
                            coordinator,
                            preparation,
                            reservation,
                        )

    def test_unpublished_owned_candidate_is_closed_then_unlinked(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                with mock.patch.object(
                    platform_fs_windows._WindowsCandidateFile,
                    "_flush_content",
                    side_effect=PlatformFileError(
                        PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                        retryable=False,
                    ),
                ):
                    with self.assertRaises(ActivationPreparationError) as caught:
                        coordinator.publish_portable_prepared_activation(
                            preparation,
                            **owner_inputs,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertEqual(list((root / private_name).iterdir()), [])
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_journal_terminal_failure_requires_recovery(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                with mock.patch.object(
                    platform_fs_windows._WindowsPendingPublication,
                    "_terminal_reproof",
                    side_effect=PlatformFileError(
                        PlatformFileErrorCode.IDENTITY_STALE,
                        retryable=True,
                    ),
                ):
                    with self.assertRaises(ActivationPreparationError) as caught:
                        coordinator.publish_portable_prepared_activation(
                            preparation,
                            **owner_inputs,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertEqual(
                    sorted(path.name for path in (root / private_name).iterdir()),
                    ["activation-journal-v3.json", "device.key"],
                )
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_owner_reproof_drift_before_journal_arm_preserves_key(self) -> None:
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
            reservation, sealed, _registry = self._portable_sealed_stage(
                service,
                coordinator,
                identity,
            )
            preparation = None
            try:
                preparation = coordinator.activate(sealed)
                owner_inputs = reservation.portable_journal_inputs()
                private_name = (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                borrow_type = type(preparation._physical_snapshot.live_reproof)
                real_reprove = borrow_type.reprove
                calls = 0

                def fail_fourth(borrow: object) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 4:
                        raise tm_stage_sealer.StageSealError(
                            "SEALER.ARTIFACT_MUTATED"
                        )
                    real_reprove(borrow)

                with mock.patch.object(
                    borrow_type,
                    "reprove",
                    new=fail_fourth,
                ):
                    with self.assertRaises(ActivationPreparationError) as caught:
                        coordinator.publish_portable_prepared_activation(
                            preparation,
                            **owner_inputs,
                        )
                self.assertEqual(calls, 4)
                self.assertEqual(
                    caught.exception.code,
                    "ACTIVATION.RECOVERY_REQUIRED",
                )
                self.assertEqual(
                    sorted(path.name for path in (root / private_name).iterdir()),
                    ["device.key"],
                )
                reservation.reprove()
            finally:
                _release_portable_test_authorities(
                    coordinator,
                    preparation,
                    reservation,
                )

    def test_stage_specific_creator_pair_spans_gate_b_token_and_cancel(self) -> None:
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
            reservation = service._acquire_initial_reservation()
            attempt = None
            sealed = None
            token = None
            try:
                preflight = service.preflight(identity.configured_jsonl_path)
                path_salt = "initial-stage-specific-gate-b"
                attempt_stage = tm_migration._deterministic_stage_ref(
                    identity,
                    source_digest=preflight.source_digest,
                    stage_prefix="migration",
                    path_salt=path_salt,
                )
                attempt = tm_migration._freeze_initial_stage_attempt(
                    attempt_stage,
                    path_salt=path_salt,
                    resource_reservation=reservation,
                )
                stage, _stage_identity, _manifest_identity = service._build_stage(
                    identity.configured_jsonl_path,
                    preflight=preflight,
                    canonical_store_id="store.primary",
                    batch_kind="migration",
                    batch_prefix="migration",
                    snapshot_prefix="snapshot.migration",
                    stage_prefix="migration",
                    path_salt=path_salt,
                    initial_attempt=attempt,
                )
                sealed = coordinator._seal_stage(
                    stage,
                    canonical_store_id="store.primary",
                    expected_prior_generation=None,
                    **reservation.stage_seal_inputs(attempt),
                )
                database = attempt.stage_reservation
                manifest = attempt.manifest_reservation
                if database is None or manifest is None:
                    raise AssertionError("expected exact creator pair")
                reservation_type = type(database)
                real_identity = reservation_type.identity
                identity_calls = {id(database): 0, id(manifest): 0}

                def record_identity(owner: object) -> object:
                    if id(owner) in identity_calls:
                        identity_calls[id(owner)] += 1
                    return real_identity(owner)

                def require_pair_reproof(action: object) -> object:
                    before = dict(identity_calls)
                    result = action()
                    self.assertGreater(
                        identity_calls[id(database)],
                        before[id(database)],
                    )
                    self.assertGreater(
                        identity_calls[id(manifest)],
                        before[id(manifest)],
                    )
                    return result

                registry = coordinator._sealed_registry
                with mock.patch.object(
                    reservation_type,
                    "identity",
                    new=record_identity,
                ):
                    for _pass in range(2):
                        report = require_pair_reproof(
                            lambda: GateBEvaluator(
                                registry=registry._readiness_view()
                            ).evaluate(sealed)
                        )
                        self.assertTrue(report.granted)
                    token = require_pair_reproof(
                        lambda: registry.issue_token(
                            sealed,
                            current_generation=None,
                        )
                    )
                    require_pair_reproof(lambda: registry.cancel(token))

                self.assertFalse(database.closed)
                self.assertFalse(manifest.closed)
                self.assertFalse(reservation._root.closed)
                self.assertFalse(reservation._lease.closed)
                database.identity()
                manifest.identity()
                tm_migration._cleanup_initial_unpublished_stage(attempt)
                self.assertEqual(list(root.glob(".localcat-migration.initial-*")), [])
            finally:
                if sealed is not None:
                    entry = coordinator._sealed_registry._entries.get(
                        sealed.artifact.artifact_id
                    )
                    if entry is not None and entry.live_authority is not None:
                        if token is None:
                            coordinator._sealed_registry.retire_unissued_portable(
                                sealed
                            )
                        else:
                            coordinator._sealed_registry.cancel(token)
                if attempt is not None:
                    if any(
                        value is not None
                        for value in (
                            attempt.stage_reservation,
                            attempt.manifest_reservation,
                        )
                    ):
                        tm_migration._cleanup_initial_unpublished_stage(attempt)
                    tm_migration._close_initial_stage_attempt_authorities(attempt)
                reservation._initial_attempt = None
                reservation.release()
            _remove_long_quarantine(root)

    def test_real_initial_build_and_seal_retire_exact_pair_before_journal(self) -> None:
        """The production entry reaches portable seal and B cleanup on denial."""

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
            self.assertTrue(
                tm_sqlite_store.detect_sqlite_runtime().fts5_available
            )
            with (
                mock.patch.object(
                    coordinator,
                    "activate",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.GATE_B_DENIED",
                        retryable=False,
                    ),
                ) as activate,
                mock.patch.object(
                    coordinator,
                    "publish_activation",
                ) as publish,
                mock.patch.object(
                    service,
                    "_verify_initial_activation_runtime",
                ) as verify,
                mock.patch.object(
                    tm_migration._ExportParentHandle,
                    "bind",
                    side_effect=AssertionError("POSIX export parent used"),
                ),
                mock.patch.object(
                    tm_stage_sealer,
                    "_capture_content_file",
                    side_effect=AssertionError("legacy POSIX capture used"),
                ),
                mock.patch.object(
                    tm_migration,
                    "_exclusive_initial_quarantine_move",
                    side_effect=AssertionError("POSIX quarantine move used"),
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "ACTIVATION.ATTESTATION_UNAVAILABLE",
            )
            activate.assert_not_called()
            publish.assert_not_called()
            verify.assert_not_called()
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(coordinator._sealed_registry._sealed_paths, {})
            self.assertEqual(
                list(root.glob(".localcat-migration.initial-*.stage")),
                [],
            )
            self.assertEqual(
                list(root.glob(".localcat-migration.initial-*.manifest.tmp")),
                [],
            )
            quarantined = list(
                (root / ".localcat-activation-quarantine-v1").glob(
                    "initial-*/*"
                )
            )
            self.assertEqual(len(quarantined), 2)
            self.assertEqual(
                {path.suffix for path in quarantined},
                {".stage", ".tmp"},
            )
            _remove_long_quarantine(root)

    def test_sealer_rejects_opened_handle_that_is_not_creator_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            foreign_path = root / "foreign.sqlite3"
            foreign_bytes = b"foreign-stage-must-not-be-adopted"
            foreign_path.write_bytes(foreign_bytes)
            backend = compose_platform_file_backend(root)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=coordinator,
                platform_backend=backend,
            )
            backend_type = type(backend)
            real_open = backend_type.open_existing_for_synchronization
            diverted = False

            def divert_database(
                instance: object,
                rooted_parent: object,
                relative: PurePath,
            ) -> object:
                nonlocal diverted
                if not diverted and str(relative).endswith(".sqlite3.stage"):
                    diverted = True
                    return real_open(
                        instance,
                        rooted_parent,
                        PurePath(foreign_path.name),
                    )
                return real_open(instance, rooted_parent, relative)

            with mock.patch.object(
                backend_type,
                "open_existing_for_synchronization",
                new=divert_database,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertTrue(diverted)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(outcome.error_code, "SEALER.ARTIFACT_MUTATED")
            self.assertEqual(foreign_path.read_bytes(), foreign_bytes)
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(
                list(root.glob(".localcat-migration.initial-*")),
                [],
            )
            _remove_long_quarantine(root)

    def test_sqlite_caller_reservation_failure_never_unlinks_or_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            stage = tm_migration._deterministic_stage_ref(
                identity,
                source_digest=hashlib.sha256(SOURCE_BYTES).hexdigest(),
                stage_prefix="migration",
                path_salt="initial-sqlite-reservation-test",
            )
            backend = compose_platform_file_backend(root)
            parent = backend.bind_root(root)
            reservation = backend.reserve_mutable_file(
                parent,
                stage.staged_db_path.name,
            )
            try:
                with (
                    mock.patch.object(
                        tm_sqlite_store,
                        "_SCHEMA_STATEMENTS",
                        ("THIS IS NOT SQL",),
                    ),
                    self.assertRaises(sqlite3.DatabaseError),
                ):
                    tm_sqlite_store.initialize_stage_schema(
                        stage,
                        canonical_store_id="store.primary",
                        _caller_reservation=reservation,
                        _caller_parent=parent,
                    )
                self.assertFalse(reservation.closed)
                reservation.identity()
                self.assertIsNotNone(
                    parent.inspect_entry(stage.staged_db_path.name)
                )
            finally:
                reservation.close()
                parent.close()
            stage.staged_db_path.unlink()

    def test_second_create_new_failure_retires_first_owner_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = _identity(root)
            backend = compose_platform_file_backend(root)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            service = TMMigrationService(
                resource_identity=identity,
                canonical_store_id="store.primary",
                coordinator=coordinator,
                platform_backend=backend,
            )
            backend_type = type(backend)
            real_reserve = backend_type._reserve_mutable_file
            calls = 0

            def fail_second(
                instance: object,
                parent: object,
                name: str,
            ) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PlatformFileError(
                        PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                        retryable=False,
                    )
                return real_reserve(instance, parent, name)

            with mock.patch.object(
                backend_type,
                "_reserve_mutable_file",
                new=fail_second,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(calls, 2)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_STAGE_CONFLICT",
            )
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(
                list(root.glob(".localcat-migration.initial-*")),
                [],
            )
            quarantined = list(
                (root / ".localcat-activation-quarantine-v1").glob(
                    "initial-*/*"
                )
            )
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].suffix, ".stage")
            _remove_long_quarantine(root)

    def test_post_seal_reservation_failure_retires_before_cleanup(self) -> None:
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
            reservation_type = tm_migration._InitialActivationResourceReservation
            real_reprove = reservation_type.reprove
            failed = False

            def fail_once_after_seal(reservation: object) -> None:
                nonlocal failed
                if coordinator._sealed_registry._entries and not failed:
                    failed = True
                    raise tm_migration._InitialActivationReservationError(
                        "MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE"
                    )
                real_reprove(reservation)

            with mock.patch.object(
                reservation_type,
                "reprove",
                new=fail_once_after_seal,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertTrue(failed)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE",
            )
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(list(root.glob(".localcat-migration.initial-*")), [])
            self.assertEqual(
                len(
                    list(
                        (root / ".localcat-activation-quarantine-v1").glob(
                            "initial-*/*"
                        )
                    )
                ),
                2,
            )
            _remove_long_quarantine(root)

    def test_missing_required_registry_retirement_fails_closed(self) -> None:
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
            reservation_type = tm_migration._InitialActivationResourceReservation
            registry_type = type(coordinator._sealed_registry)
            real_reprove = reservation_type.reprove
            real_retire = registry_type.retire_unissued_portable
            failed = False
            sealed_values: list[object] = []
            real_seal = coordinator._seal_stage

            def record_seal(*args: object, **kwargs: object) -> object:
                sealed = real_seal(*args, **kwargs)
                sealed_values.append(sealed)
                return sealed

            def fail_once_after_seal(reservation: object) -> None:
                nonlocal failed
                if sealed_values and not failed:
                    failed = True
                    raise tm_migration._InitialActivationReservationError(
                        "MIGRATION.INITIAL_RESOURCE_LOCK_UNAVAILABLE"
                    )
                real_reprove(reservation)

            def refuse_retirement(instance: object, sealed: object) -> bool:
                del instance, sealed
                return False

            with (
                mock.patch.object(
                    coordinator,
                    "_seal_stage",
                    side_effect=record_seal,
                ),
                mock.patch.object(
                    reservation_type,
                    "reprove",
                    new=fail_once_after_seal,
                ),
                mock.patch.object(
                    registry_type,
                    "retire_unissued_portable",
                    new=refuse_retirement,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertTrue(failed)
            self.assertEqual(len(sealed_values), 1)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertNotEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(
                len(list(root.glob(".localcat-migration.initial-*"))),
                2,
            )
            real_retire(coordinator._sealed_registry, sealed_values[0])
            for path in root.glob(".localcat-migration.initial-*"):
                path.unlink()

    def test_retirement_programmer_faults_cross_without_consuming_owner(self) -> None:
        for fault in (TypeError, AssertionError, AttributeError):
            with self.subTest(fault=fault.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    identity = _identity(root)
                    backend = compose_platform_file_backend(root)
                    coordinator = ResourceStoreCoordinator(
                        canonical_store_id="store.primary",
                        resource_identity=identity,
                    )
                    service = TMMigrationService(
                        resource_identity=identity,
                        canonical_store_id="store.primary",
                        coordinator=coordinator,
                        platform_backend=backend,
                    )
                    backend_type = type(backend)
                    observed_open: list[bool] = []

                    def fail_retirement(
                        instance: object,
                        source_parent: object,
                        source_name: str,
                        reservation: object,
                        target_parent: object,
                        target_name: str,
                    ) -> object:
                        del (
                            instance,
                            source_parent,
                            source_name,
                            target_parent,
                            target_name,
                        )
                        observed_open.append(not reservation.closed)
                        raise fault("programmer fault")

                    with (
                        mock.patch.object(
                            coordinator,
                            "activate",
                            side_effect=ActivationPreparationError(
                                "ACTIVATION.GATE_B_DENIED",
                                retryable=False,
                            ),
                        ),
                        mock.patch.object(
                            backend_type,
                            "_retire_owned_exclusive",
                            new=fail_retirement,
                        ),
                        self.assertRaises(fault),
                    ):
                        service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    self.assertEqual(observed_open, [True])
                    self.assertEqual(coordinator._sealed_registry._entries, {})
                    self.assertEqual(
                        len(list(root.glob(".localcat-migration.initial-*"))),
                        2,
                    )
                    for path in root.glob(".localcat-migration.initial-*"):
                        path.unlink()
                    _remove_long_quarantine(root)

    def test_retirement_close_failure_fails_closed_as_authority_unavailable(
        self,
    ) -> None:
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
            retirement_type = platform_fs_windows._WindowsRetainedRetirement
            real_close = retirement_type._close_retirement_authority
            close_calls = 0

            def close_then_fail(retirement: object) -> None:
                nonlocal close_calls
                real_close(retirement)
                close_calls += 1
                raise PlatformFileError(
                    PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                )

            with (
                mock.patch.object(
                    coordinator,
                    "activate",
                    side_effect=ActivationPreparationError(
                        "ACTIVATION.GATE_B_DENIED",
                        retryable=False,
                    ),
                ),
                mock.patch.object(
                    retirement_type,
                    "_close_retirement_authority",
                    new=close_then_fail,
                ),
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(close_calls, 2)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(list(root.glob(".localcat-migration.initial-*")), [])
            _remove_long_quarantine(root)

    def test_retirement_close_programmer_faults_cross_public_seam(self) -> None:
        for fault in (TypeError, AssertionError, AttributeError):
            with self.subTest(fault=fault.__name__):
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
                    retirement_type = (
                        platform_fs_windows._WindowsRetainedRetirement
                    )
                    real_close = retirement_type._close_retirement_authority

                    def close_then_raise(retirement: object) -> None:
                        real_close(retirement)
                        raise fault("programmer close fault")

                    with (
                        mock.patch.object(
                            retirement_type,
                            "_close_retirement_authority",
                            new=close_then_raise,
                        ),
                        self.assertRaisesRegex(
                            fault,
                            "programmer close fault",
                        ),
                    ):
                        service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                    self.assertEqual(
                        coordinator._sealed_registry._entries,
                        {},
                    )
                    self.assertEqual(
                        list(root.glob(".localcat-migration.initial-*")),
                        [],
                    )
                    _remove_long_quarantine(root)

    def test_programmer_primary_survives_attempt_authority_close_failure(
        self,
    ) -> None:
        reservation_type = platform_fs_windows._WindowsMutableFileReservation
        real_close = reservation_type._close_authority
        for fault in (TypeError, AssertionError, AttributeError):
            with self.subTest(fault=fault.__name__):
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
                    close_calls = 0

                    def close_then_fail(authority: object) -> None:
                        nonlocal close_calls
                        real_close(authority)
                        close_calls += 1
                        raise PlatformFileError(
                            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                            retryable=False,
                        )

                    with (
                        mock.patch.object(
                            service,
                            "_build_stage",
                            side_effect=fault("programmer primary"),
                        ),
                        mock.patch.object(
                            reservation_type,
                            "_close_authority",
                            new=close_then_fail,
                        ),
                        self.assertRaisesRegex(fault, "programmer primary"),
                    ):
                        service.activate_initial(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                    self.assertEqual(close_calls, 2)
                    self.assertEqual(
                        coordinator._sealed_registry._entries,
                        {},
                    )
                    for path in root.glob(".localcat-migration.initial-*"):
                        path.unlink()

    def test_legacy_source_close_failure_is_authority_unavailable(self) -> None:
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
            regular_type = platform_fs_windows._WindowsBoundRegularFile
            real_close = regular_type._close_authority
            exact_close_calls = 0

            def close_then_fail_terminal(authority: object) -> None:
                nonlocal exact_close_calls
                real_close(authority)
                if type(authority) is regular_type:
                    exact_close_calls += 1
                    if exact_close_calls == 2:
                        raise PlatformFileError(
                            PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
                            retryable=False,
                        )

            with mock.patch.object(
                regular_type,
                "_close_authority",
                new=close_then_fail_terminal,
            ):
                outcome = service.activate_initial(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

            self.assertEqual(exact_close_calls, 2)
            self.assertIs(type(outcome), tm_migration.MigrationFailure)
            self.assertEqual(
                outcome.error_code,
                "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            )
            self.assertTrue(outcome.canonical_authority_ambiguous)
            self.assertEqual(coordinator._sealed_registry._entries, {})
            self.assertEqual(list(root.glob(".localcat-migration.initial-*")), [])
            _remove_long_quarantine(root)

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
