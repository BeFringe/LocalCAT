"""Focused Windows owner/recovery checks for WA-06 5.10a replacement."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import tm_activation_journal
import tm_activation_recovery
import tm_migration
import tm_sqlite_store
import tm_stage_sealer
from platform_fs_windows import WindowsPlatformAdapter
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    SourceBindingState,
)
from tm_engine import TMEngine
from tm_migration import TMMigrationService
from tm_sqlite_store import ActivationPreparationError, ResourceStoreCoordinator


_INITIAL_SOURCE = b'{"source":"same","target":"old"}\n'
_REPLACEMENT_SOURCE = b'{"source":"same","target":"new"}\n'
_SECOND_REPLACEMENT_SOURCE = b'{"source":"same","target":"newer"}\n'


def _fixture(
    root: Path,
    *,
    platform_backend: WindowsPlatformAdapter | None = None,
) -> tuple[CanonicalResourceIdentity, ResourceStoreCoordinator, TMMigrationService]:
    source = (root / "tm.primary.jsonl").resolve()
    source.write_bytes(_INITIAL_SOURCE)
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
        platform_backend=platform_backend,
    )
    outcome = service.activate_initial(source, identity.resource_id)
    if type(outcome) is not MigrationReport:
        raise AssertionError(f"initial activation failed: {outcome!r}")
    source.write_bytes(_REPLACEMENT_SOURCE)
    return identity, coordinator, service


def _fresh(
    identity: CanonicalResourceIdentity,
) -> tuple[ResourceStoreCoordinator, TMMigrationService]:
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.primary",
        resource_identity=identity,
    )
    service = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.primary",
        coordinator=coordinator,
    )
    return coordinator, service


def _recover(
    coordinator: ResourceStoreCoordinator,
    service: TMMigrationService,
) -> object:
    with service._acquire_initial_reservation() as reservation:
        return coordinator.recover_portable_replacement_activation(
            **reservation.portable_replacement_recovery_inputs()
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


def _remove_long_private(root: Path) -> None:
    for private_root in root.glob(".localcat-activation-private-v1.*"):
        extended = "\\\\?\\" + str(private_root)
        for name in os.listdir(extended):
            os.unlink(os.path.join(extended, name))
        os.rmdir(extended)


def _replacement_bytes(private_root: Path) -> dict[str, bytes]:
    extended = "\\\\?\\" + str(private_root)
    result: dict[str, bytes] = {}
    for name in os.listdir(extended):
        if name.startswith("activation-replacement-") or name == "foreign.bin":
            with open(os.path.join(extended, name), "rb") as stream:
                result[name] = stream.read()
    return result


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableReplacementOwnerTests(unittest.TestCase):
    def test_replacement_reuses_sealed_projection_after_full_predecessor_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _fixture(root)
                real_sealer_validation = (
                    tm_stage_sealer._validate_candidate_proof_index_with_digest
                )
                real_predecessor_validation = (
                    tm_sqlite_store.validate_candidate_proof_index
                )
                real_projection_digest = (
                    tm_sqlite_store._candidate_proof_projection_digest
                )
                with (
                    mock.patch.object(
                        tm_stage_sealer,
                        "_validate_candidate_proof_index_with_digest",
                        wraps=real_sealer_validation,
                    ) as sealer_validation,
                    mock.patch.object(
                        tm_sqlite_store,
                        "validate_candidate_proof_index",
                        wraps=real_predecessor_validation,
                    ) as predecessor_validation,
                    mock.patch.object(
                        tm_sqlite_store,
                        "_validate_activation_indexes",
                        side_effect=AssertionError(
                            "replacement candidate must reuse the sealed projection"
                        ),
                    ) as active_full_validation,
                    mock.patch.object(
                        tm_sqlite_store,
                        "_candidate_proof_projection_digest",
                        wraps=real_projection_digest,
                    ) as active_projection_digest,
                ):
                    outcome = service._explicit_disambiguation(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(outcome), MigrationReport)
                self.assertEqual(outcome.activated_generation, 1)
                sealer_validation.assert_called_once()
                predecessor_validation.assert_called_once()
                active_full_validation.assert_not_called()
                active_projection_digest.assert_called_once()
                self.assertEqual(coordinator.current_generation, 1)
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_prior_backups_close_before_prepared_and_ready_adopts_n_plus_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _fixture(root)
                private_root = root / (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                phases: list[str] = []
                real_publish = (
                    tm_activation_recovery._PortableReplacementRecordOwner.publish
                )

                def observed_publish(**kwargs: object) -> object:
                    unsigned = kwargs["unsigned"]
                    phase = unsigned.phase
                    if phase == "PREPARED":
                        for backup in unsigned.backup_proofs:
                            with open(
                                "\\\\?\\"
                                + str(private_root / backup.backup_name),
                                "rb",
                            ) as stream:
                                payload = stream.read()
                            self.assertEqual(len(payload), backup.content.size)
                            self.assertEqual(
                                hashlib.sha256(payload).hexdigest(),
                                backup.content.sha256,
                            )
                    record = real_publish(**kwargs)
                    phases.append(phase)
                    return record

                with mock.patch.object(
                    tm_activation_recovery._PortableReplacementRecordOwner,
                    "publish",
                    side_effect=observed_publish,
                ):
                    outcome = service._explicit_disambiguation(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )

                self.assertIs(type(outcome), MigrationReport)
                self.assertEqual(outcome.activated_generation, 1)
                self.assertEqual(coordinator.current_generation, 1)
                self.assertEqual(coordinator.state, "READY")
                self.assertEqual(
                    phases,
                    [
                        "PREPARED",
                        "DB_REPLACED",
                        "MANIFEST_PUBLISHED",
                        "GENERATION_PUBLISHED",
                        "READY",
                    ],
                )
                replacement_names = {
                    name
                    for name in os.listdir("\\\\?\\" + str(private_root))
                    if name.startswith("activation-replacement-")
                }
                self.assertEqual(
                    replacement_names,
                    {tm_activation_journal._PORTABLE_REPLACEMENT_CURRENT_NAME},
                )
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_two_consecutive_replacements_advance_to_generation_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _fixture(root)
                first = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(first), MigrationReport)
                self.assertEqual(first.activated_generation, 1)

                identity.configured_jsonl_path.write_bytes(
                    _SECOND_REPLACEMENT_SOURCE
                )
                second = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )

                self.assertIs(type(second), MigrationReport)
                self.assertEqual(second.activated_generation, 2)
                self.assertEqual(coordinator.current_generation, 2)
                self.assertEqual(coordinator.state, "READY")
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_backup_publish_faults_retire_only_exact_owned_names(self) -> None:
        for checkpoint, fail_on in (
            ("publish_before_rename", 2),
            ("publish_after_rename", 1),
        ):
            with self.subTest(checkpoint=checkpoint):
                state = {"armed": False, "count": 0}

                def inject(phase: str) -> None:
                    if not state["armed"] or phase != checkpoint:
                        return
                    state["count"] += 1
                    if state["count"] == fail_on:
                        state["armed"] = False
                        raise RuntimeError("replacement backup publish fault")

                backend = WindowsPlatformAdapter(_fault_injector=inject)
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    try:
                        identity, coordinator, service = _fixture(
                            root,
                            platform_backend=backend,
                        )
                        prior_database = (
                            identity.canonical_sidecar_path.read_bytes()
                        )
                        source_bytes = identity.configured_jsonl_path.read_bytes()
                        foreign = root / "foreign-owned.bin"
                        foreign.write_bytes(b"foreign-owned\n")
                        private_root = root / (
                            tm_activation_journal._portable_activation_private_directory_name(
                                identity
                            )
                        )

                        state["armed"] = True
                        outcome = service._explicit_disambiguation(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )

                        self.assertIs(
                            type(outcome),
                            tm_migration.MigrationFailure,
                            repr(outcome),
                        )
                        self.assertEqual(coordinator.current_generation, 0)
                        self.assertEqual(coordinator.state, "READY")
                        self.assertEqual(_replacement_bytes(private_root), {})
                        self.assertEqual(
                            identity.canonical_sidecar_path.read_bytes(),
                            prior_database,
                        )
                        self.assertEqual(
                            identity.configured_jsonl_path.read_bytes(),
                            source_bytes,
                        )
                        self.assertEqual(
                            foreign.read_bytes(),
                            b"foreign-owned\n",
                        )

                        retried = service._explicit_disambiguation(
                            identity.configured_jsonl_path,
                            identity.resource_id,
                        )
                        self.assertIs(type(retried), MigrationReport, repr(retried))
                        self.assertEqual(retried.activated_generation, 1)
                    finally:
                        _remove_long_quarantine(root)
                        _remove_long_private(root)

    def test_public_cold_open_adopts_terminal_generation_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport, repr(outcome))
                self.assertEqual(outcome.activated_generation, 1)

                reopened = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                    expected_resource_id=identity.resource_id,
                )

                self.assertTrue(reopened.canonical_active)
                store = reopened.canonical_store
                assert store is not None
                self.assertEqual(store.canonical_revision().generation, 1)
                match = reopened.query_exact("same")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.target, "new")
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_public_cold_open_adopts_terminal_generation_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                first = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(first), MigrationReport, repr(first))
                identity.configured_jsonl_path.write_bytes(
                    _SECOND_REPLACEMENT_SOURCE
                )
                second = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(second), MigrationReport, repr(second))
                self.assertEqual(second.activated_generation, 2)

                reopened = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                    expected_resource_id=identity.resource_id,
                )

                self.assertTrue(reopened.canonical_active)
                store = reopened.canonical_store
                assert store is not None
                self.assertEqual(store.canonical_revision().generation, 2)
                match = reopened.query_exact("same")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.target, "newer")
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_public_cold_open_observes_source_divergence_then_imports_gen_two(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                first = service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(first), MigrationReport, repr(first))
                self.assertEqual(first.activated_generation, 1)
                identity.configured_jsonl_path.write_bytes(
                    _SECOND_REPLACEMENT_SOURCE
                )

                reopened = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                    expected_resource_id=identity.resource_id,
                )

                self.assertTrue(reopened.canonical_active)
                store = reopened.canonical_store
                assert store is not None
                self.assertEqual(store.canonical_revision().generation, 1)
                self.assertEqual(
                    store.source_binding_monitor.observe().state,
                    SourceBindingState.SOURCE_DIVERGED,
                )

                fresh_coordinator, fresh_service = _fresh(identity)
                self.assertIsNone(fresh_coordinator.current_generation)
                self.assertIsNone(fresh_coordinator._view)

                second = fresh_service.import_snapshot(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(second), MigrationReport, repr(second))
                self.assertEqual(second.activated_generation, 2)
                self.assertEqual(fresh_coordinator.current_generation, 2)

                final = TMEngine(
                    str(identity.configured_jsonl_path),
                    update=False,
                    expected_resource_id=identity.resource_id,
                )
                self.assertTrue(final.canonical_active)
                final_store = final.canonical_store
                assert final_store is not None
                self.assertEqual(final_store.canonical_revision().generation, 2)
                match = final.query_exact("same")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertEqual(match.target, "newer")
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_fresh_recovery_adopts_terminal_new_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, _coordinator, service = _fixture(root)
                outcome = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(outcome), MigrationReport)

                fresh, fresh_service = _fresh(identity)
                report = _recover(fresh, fresh_service)

                self.assertIsNotNone(report)
                self.assertEqual(report.action, "COMPLETED")
                self.assertEqual(report.generation, 1)
                self.assertEqual(fresh.current_generation, 1)
                self.assertEqual(fresh.state, "READY")
                self.assertEqual(
                    fresh.canonical_store_id,
                    outcome.canonical_store_id,
                )
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_fresh_recovery_adopts_prior_after_prepared_owner_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _fixture(root)
                real_publish = (
                    tm_activation_recovery._PortableReplacementRecordOwner.publish
                )

                def fault_after_prepared(**kwargs: object) -> object:
                    record = real_publish(**kwargs)
                    if kwargs["unsigned"].phase == "PREPARED":
                        raise PlatformFileError(
                            PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                            retryable=False,
                        )
                    return record

                with mock.patch.object(
                    tm_activation_recovery._PortableReplacementRecordOwner,
                    "publish",
                    side_effect=fault_after_prepared,
                ):
                    outcome = service._explicit_disambiguation(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(outcome), tm_migration.MigrationFailure)
                self.assertEqual(coordinator.current_generation, 0)
                self.assertEqual(coordinator.state, "READY")
                private_root = root / (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                self.assertEqual(_replacement_bytes(private_root), {})

                fresh, fresh_service = _fresh(identity)
                report = _recover(fresh, fresh_service)
                self.assertIsNotNone(report)
                self.assertEqual(report.action, "COMPLETED")
                self.assertEqual(report.generation, 0)
                self.assertEqual(fresh.current_generation, 0)
                self.assertEqual(fresh.state, "READY")

                retried = service._explicit_disambiguation(
                    identity.configured_jsonl_path,
                    identity.resource_id,
                )
                self.assertIs(type(retried), MigrationReport, repr(retried))
                self.assertEqual(retried.activated_generation, 1)
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            try:
                identity, coordinator, service = _fixture(root)
                real_publish = (
                    tm_activation_recovery._PortableReplacementRecordOwner.publish
                )
                real_recovery = (
                    coordinator.recover_portable_replacement_activation
                )
                recovery_calls = 0

                def fault_after_prepared(**kwargs: object) -> object:
                    record = real_publish(**kwargs)
                    if kwargs["unsigned"].phase == "PREPARED":
                        raise PlatformFileError(
                            PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                            retryable=False,
                        )
                    return record

                def defer_post_fault_recovery(**kwargs: object) -> object:
                    nonlocal recovery_calls
                    recovery_calls += 1
                    if recovery_calls == 1:
                        return real_recovery(**kwargs)
                    raise ActivationPreparationError(
                        "ACTIVATION.RECOVERY_REQUIRED",
                        retryable=True,
                    )

                with (
                    mock.patch.object(
                        tm_activation_recovery._PortableReplacementRecordOwner,
                        "publish",
                        side_effect=fault_after_prepared,
                    ),
                    mock.patch.object(
                        coordinator,
                        "recover_portable_replacement_activation",
                        side_effect=defer_post_fault_recovery,
                    ),
                ):
                    outcome = service._explicit_disambiguation(
                        identity.configured_jsonl_path,
                        identity.resource_id,
                    )
                self.assertIs(type(outcome), tm_migration.MigrationFailure)
                private_root = root / (
                    tm_activation_journal._portable_activation_private_directory_name(
                        identity
                    )
                )
                foreign_path = private_root / "foreign.bin"
                foreign_path.write_bytes(b"foreign-owned\n")
                before = _replacement_bytes(private_root)

                fresh, fresh_service = _fresh(identity)
                with self.assertRaises(ActivationPreparationError):
                    _recover(fresh, fresh_service)

                self.assertEqual(_replacement_bytes(private_root), before)
                self.assertEqual(foreign_path.read_bytes(), b"foreign-owned\n")
            finally:
                _remove_long_quarantine(root)
                _remove_long_private(root)

    def test_operational_fault_is_recovery_required_but_programmer_error_passes(
        self,
    ) -> None:
        for error in (
            PlatformFileError(
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
                retryable=False,
            ),
            TypeError("programmer fault"),
        ):
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    try:
                        identity, _coordinator, service = _fixture(root)
                        with mock.patch.object(
                            tm_activation_recovery._PortableReplacementRecordOwner,
                            "publish",
                            side_effect=error,
                        ):
                            if isinstance(error, TypeError):
                                with self.assertRaisesRegex(
                                    TypeError,
                                    "programmer fault",
                                ):
                                    service._explicit_disambiguation(
                                        identity.configured_jsonl_path,
                                        identity.resource_id,
                                    )
                            else:
                                outcome = service._explicit_disambiguation(
                                    identity.configured_jsonl_path,
                                    identity.resource_id,
                                )
                                self.assertIs(
                                    type(outcome),
                                    tm_migration.MigrationFailure,
                                )
                    finally:
                        _remove_long_quarantine(root)
                        _remove_long_private(root)


if __name__ == "__main__":
    unittest.main()
