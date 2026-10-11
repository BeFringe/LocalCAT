from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from resource_package_contracts import (
    ResourceImportMode, ResourcePortabilityError, ResourceRecoveryAction,
    ResourceRecoveryDisposition,
)
from resource_portability import ResourcePortabilityService
from resource_receipt_ledger import ResourcePendingPhase
from resource_repository import ResourceError, ResourceRepository
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_engine import TMEngine
from tm_migration import (
    TMMigrationService,
    _InitialActivationReservationError,
    _InitialActivationResourceReservation,
)
from tm_resource_port import TMResourceSnapshotPort
from tm_sqlite_store import ResourceStoreCoordinator


@unittest.skipUnless(os.name == "posix", "POSIX snapshot owner transaction")
class TMResourceSnapshotPosixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source_repository = ResourceRepository(self.root / "source")
        self.source = self._active_resource(self.source_repository, "incoming", "新")
        self.package = self.root / "tm.localcat-resource"
        self.exported = ResourcePortabilityService(self.source_repository).export_package(
            self.source.id, self.package,
        )
        self.package_before = self.package.read_bytes()
        self.payload = self.root / "payload.jsonl"
        self.payload.write_bytes(self.source.path.read_bytes())
        self.repository = ResourceRepository(self.root / "receiver")
        self.service = ResourcePortabilityService(self.repository)

    def _active_resource(self, repository, source, target):
        resource = repository.create_resource(source, ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_text(
            json.dumps({"source": source, "target": target}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        coordinator = ResourceStoreCoordinator(
            canonical_store_id=f"store.{resource.id}", resource_identity=identity,
        )
        outcome = TMMigrationService(
            resource_identity=identity,
            canonical_store_id=coordinator.canonical_store_id,
            coordinator=coordinator,
        ).activate_initial(resource.path, resource.id)
        self.assertIs(type(outcome), MigrationReport)
        return resource

    def _assert_locked(self, resource):
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        with self.assertRaises(_InitialActivationReservationError):
            with _InitialActivationResourceReservation.acquire_with_backend(
                identity, self.repository.platform_backend,
            ):
                self.fail("a competing owner acquired W1 during the callback")

    def _assert_cold(self, result, generation):
        cold_repository = ResourceRepository(self.root / "receiver")
        resource = cold_repository.get(result.destination_resource_id)
        cold = ResourcePortabilityService(cold_repository)
        facts = cold._tm.reopen_snapshot(resource, result.receipt)
        self.assertEqual(facts.owner_generation, generation)
        self.assertEqual(facts.record_count, 1)
        engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
        self.assertEqual(engine.query_exact("incoming").target, "新")
        self.assertIsNone(engine.query_exact("old"))
        self.assertIn(result.receipt, cold._ledger.list_receipts())
        self.assertEqual(cold._ledger.list_pending(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        return resource

    def test_create_package_callback_receipt_and_cold_open(self):
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.CREATE_NEW, new_resource_name="imported",
        )
        real_publish = self.repository.publish_prepared_create
        observed = []

        def publish(prepared):
            self._assert_locked(prepared.resource)
            self.assertEqual(self.repository.list_resources(), ())
            self.assertEqual(self.service._ledger.list_pending()[0].phase,
                             ResourcePendingPhase.RECEIPT_READY)
            self.assertEqual(self.service._ledger.list_receipts(), ())
            observed.append(prepared.resource.id)
            return real_publish(prepared)

        with patch.object(self.repository, "publish_prepared_create", side_effect=publish):
            result = self.service.apply_resource_package_import(preview)
        self.assertEqual(observed, [result.destination_resource_id])
        self.assertNotEqual(result.destination_resource_id, self.source.id)
        self._assert_cold(result, 0)

    def test_replace_package_prior_copy_callback_and_cold_open(self):
        resource = self._active_resource(self.repository, "old", "旧")
        old_store = TMEngine(str(resource.path), expected_resource_id=resource.id).canonical_store
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.REPLACE_SELECTED,
            destination_resource_id=resource.id,
        )
        real_ready = self.service._mark_pending_receipt_ready
        observed = []

        def ready(receipt):
            self._assert_locked(resource)
            real_ready(receipt)
            observed.append(receipt.owner_generation)
            self.assertEqual(self.service._ledger.list_receipts(), ())

        with patch.object(self.service, "_mark_pending_receipt_ready", side_effect=ready):
            result = self.service.apply_resource_package_import(preview)
        self.assertEqual(observed, [1])
        self.assertEqual(result.destination_resource_id, resource.id)
        self.assertNotEqual(
            old_store.coordinator.canonical_store_id,
            TMEngine(str(resource.path), expected_resource_id=resource.id).canonical_store.coordinator.canonical_store_id,
        )
        self._assert_cold(result, 1)

    def test_prior_copy_failure_cleans_owned_candidate_and_preserves_old(self):
        from platform_fs_posix import _PosixRootedDirectory

        resource = self._active_resource(self.repository, "old", "旧")
        before = resource.path.read_bytes()
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        original = _PosixRootedDirectory._rename_candidate
        reached = []

        def fail_prior(parent, candidate, destination, **kwargs):
            if ".localcat-import-recovery-" in destination:
                reached.append(destination)
                self._assert_locked(resource)
                raise PlatformFileError(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True)
            return original(parent, candidate, destination, **kwargs)

        with patch.object(_PosixRootedDirectory, "_rename_candidate", new=fail_prior):
            with self.assertRaises(ResourcePortabilityError):
                port.replace_snapshot(resource, self.payload, validated)
        self.assertEqual(len(reached), 1)
        self.assertEqual(resource.path.read_bytes(), before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("old").target, "旧")

    def test_source_publication_failure_cleans_candidates_under_w1(self):
        import tm_migration
        from platform_fs_posix import _PosixRootedDirectory

        resource = self._active_resource(self.repository, "old", "旧")
        before = resource.path.read_bytes()
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        original = _PosixRootedDirectory._rename_candidate
        original_cleanup = tm_migration._cleanup_bound_export_artifacts
        reached = []
        cleanup_observed = []

        def fail_source(parent, candidate, destination, **kwargs):
            if destination == resource.path.name:
                reached.append(destination)
                self._assert_locked(resource)
                raise PlatformFileError(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True)
            return original(parent, candidate, destination, **kwargs)

        def cleanup(parent, artifacts):
            self._assert_locked(resource)
            cleanup_observed.append(artifacts)
            return original_cleanup(parent, artifacts)

        with (
            patch.object(_PosixRootedDirectory, "_rename_candidate", new=fail_source),
            patch.object(tm_migration, "_cleanup_bound_export_artifacts", side_effect=cleanup),
        ):
            with self.assertRaises(ResourcePortabilityError):
                port.replace_snapshot(resource, self.payload, validated)
        self.assertEqual(len(reached), 1)
        self.assertTrue(cleanup_observed)
        self.assertEqual(resource.path.read_bytes(), before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))

    def test_stale_payload_never_publishes_or_calls_owner(self):
        prepared = self.repository.prepare_resource_create("stale", ResourceKind.TRANSLATION_MEMORY)
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        self.payload.write_bytes(b'{"source":"changed","target":"other"}\n')
        callbacks = []
        with self.assertRaises(ResourcePortabilityError):
            port.create_snapshot(prepared.resource, self.payload, validated, owner_commit=callbacks.append)
        self.assertEqual(callbacks, [])
        self.assertFalse(prepared.resource.path.exists())
        self.assertEqual(self.repository.list_resources(), ())
        self.assertFalse(tuple(prepared.resource.path.parent.glob("*.localcat-import-*")))
        self.repository.cancel_prepared_create(prepared, remove_owned_file=False)

    def test_source_failure_after_naming_restores_old_source_and_canonical(self):
        from platform_fs_posix import _PosixRootedDirectory

        resource = self._active_resource(self.repository, "old", "旧")
        before = resource.path.read_bytes()
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        original = _PosixRootedDirectory._rename_candidate
        failed = []

        def fail_after_naming(parent, candidate, destination, **kwargs):
            result = original(parent, candidate, destination, **kwargs)
            if destination == resource.path.name and not failed:
                self._assert_locked(resource)
                failed.append(destination)
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return result

        with patch.object(_PosixRootedDirectory, "_rename_candidate", new=fail_after_naming):
            with self.assertRaises(ResourcePortabilityError):
                port.replace_snapshot(resource, self.payload, validated)
        self.assertEqual(len(failed), 1)
        self.assertEqual(resource.path.read_bytes(), before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("old").target, "旧")

    def test_stale_destination_preview_preserves_current_owner(self):
        resource = self._active_resource(self.repository, "old", "旧")
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.REPLACE_SELECTED, destination_resource_id=resource.id,
        )
        changed = b'{"source":"uncommitted","target":"other"}\n'
        resource.path.write_bytes(changed)
        with self.assertRaises(ResourcePortabilityError) as caught:
            self.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.DESTINATION_STALE")
        self.assertEqual(resource.path.read_bytes(), changed)
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("old").target, "旧")
        self.assertEqual(self.service._ledger.list_receipts(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)

    def test_activation_failure_respects_prior_owner_preservation_proof(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                prepared = None
                if initial:
                    prepared = self.repository.prepare_resource_create("failed", ResourceKind.TRANSLATION_MEMORY)
                    resource = prepared.resource
                    before = None
                else:
                    resource = self._active_resource(self.repository, "old", "旧")
                    before = resource.path.read_bytes()
                port = TMResourceSnapshotPort(self.repository.platform_backend)
                validated = port.validate_snapshot(self.payload)
                callbacks = []

                def fail_activation(coordinator, *args, **kwargs):
                    self._assert_locked(resource)
                    raise OSError("injected activation failure")

                with patch.object(ResourceStoreCoordinator, "publish_prepared_activation", new=fail_activation):
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        method = port.create_snapshot if initial else port.replace_snapshot
                        method(resource, self.payload, validated, owner_commit=callbacks.append)
                self.assertEqual(callbacks, [])
                self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
                if initial:
                    self.assertFalse(resource.path.exists())
                    self.repository.cancel_prepared_create(prepared, remove_owned_file=False)
                else:
                    # A previous completed journal makes this PREPARE fault
                    # unproven to the existing replacement owner.  Respect its
                    # recovery evidence instead of guessing source rollback.
                    self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
                    self.assertEqual(resource.path.read_bytes(), self.payload.read_bytes())
                    self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("old").target, "旧")

    def test_stage_failure_restores_proven_prior_source_and_canonical(self):
        resource = self._active_resource(self.repository, "old", "旧")
        before = resource.path.read_bytes()
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        with patch.object(TMMigrationService, "_build_stage", side_effect=OSError("stage fault")):
            with self.assertRaises(ResourcePortabilityError) as caught:
                port.replace_snapshot(resource, self.payload, validated)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.APPLY_FAILED")
        self.assertEqual(resource.path.read_bytes(), before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("old").target, "旧")

    def test_parent_replacement_fails_closed_without_writing_new_parent(self):
        resource = self._active_resource(self.repository, "old", "旧")
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        moved = resource.path.parent.with_name("held-resources")
        reached = []

        def replace_parent(*args, **kwargs):
            self._assert_locked(resource)
            resource.path.parent.rename(moved)
            resource.path.parent.mkdir()
            reached.append(moved)
            raise OSError("parent was replaced")

        with patch.object(TMMigrationService, "_build_stage", side_effect=replace_parent):
            with self.assertRaises(ResourcePortabilityError) as caught:
                port.replace_snapshot(resource, self.payload, validated)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(reached, [moved])
        self.assertEqual(tuple(resource.path.parent.iterdir()), ())
        self.assertTrue((moved / resource.path.name).exists())

    def test_same_byte_parent_replacement_cannot_redirect_real_builder(self):
        import tm_migration

        for initial in (True, False):
            with self.subTest(initial=initial):
                repository = ResourceRepository(self.root / f"parent-clone-{initial}")
                if initial:
                    prepared = repository.prepare_resource_create("new", ResourceKind.TRANSLATION_MEMORY)
                    resource = prepared.resource
                else:
                    resource = self._active_resource(repository, "old", "旧")
                port = TMResourceSnapshotPort(repository.platform_backend)
                validated = port.validate_snapshot(self.payload)
                identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
                parent = resource.path.parent
                held_parent = parent.with_name("held-resources")
                real_build = TMMigrationService._build_stage
                real_initialize = tm_migration.initialize_stage_schema
                created_stages = []
                cloned_files = {}
                callbacks = []

                def move_then_build(service, *args, **kwargs):
                    parent.rename(held_parent)
                    shutil.copytree(held_parent, parent)
                    cloned_files.update({path.name: path.read_bytes() for path in parent.iterdir() if path.is_file()})
                    # The new directory has its own lock inode; obtaining it
                    # cannot authorize the first owner's path-based work.
                    with _InitialActivationResourceReservation.acquire_with_backend(identity, repository.platform_backend):
                        pass
                    return real_build(service, *args, **kwargs)

                def observe_initialize(stage, *args, **kwargs):
                    result = real_initialize(stage, *args, **kwargs)
                    created_stages.append(stage.staged_db_path)
                    return result

                with (
                    patch.object(TMMigrationService, "_build_stage", new=move_then_build),
                    patch.object(tm_migration, "initialize_stage_schema", new=observe_initialize),
                ):
                    with self.assertRaises(ResourcePortabilityError):
                        method = port.create_snapshot if initial else port.replace_snapshot
                        method(resource, self.payload, validated, owner_commit=callbacks.append)
                self.assertEqual(callbacks, [])
                self.assertEqual(created_stages, [], "the replacement directory must not receive a stage")
                self.assertEqual(
                    {path.name: path.read_bytes() for path in parent.iterdir() if path.is_file()},
                    cloned_files,
                )

    def test_create_registry_callback_failure_completes_from_cold_owner(self):
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.CREATE_NEW, new_resource_name="recoverable",
        )
        reached = []

        def fail_publish(prepared):
            self._assert_locked(prepared.resource)
            reached.append(prepared.resource.id)
            raise ResourceError("injected registry failure")

        with patch.object(self.repository, "publish_prepared_create", side_effect=fail_publish):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(len(reached), 1)
        self.assertEqual(self.repository.list_resources(), ())
        cold = ResourcePortabilityService(ResourceRepository(self.root / "receiver"))
        recovery = cold.inspect_resource_portability_recovery()[0]
        self.assertIs(recovery.disposition, ResourceRecoveryDisposition.COMPLETE_AVAILABLE)
        completed = cold.recover_resource_portability(recovery, ResourceRecoveryAction.COMPLETE)
        resource = cold.repository.get(completed.receipt.destination_resource_id)
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("incoming").target, "新")
        self.assertEqual(cold._ledger.list_pending(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)

    def test_unproven_replacement_rollback_leaves_source_for_owner_recovery(self):
        resource = self._active_resource(self.repository, "old", "旧")
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        validated = port.validate_snapshot(self.payload)
        callbacks = []
        with (
            patch.object(ResourceStoreCoordinator, "publish_activation", side_effect=OSError("publication fault")),
            patch.object(ResourceStoreCoordinator, "rollback_durable_activation", side_effect=OSError("rollback fault")),
        ):
            with self.assertRaises(ResourcePortabilityError) as caught:
                port.replace_snapshot(resource, self.payload, validated, owner_commit=callbacks.append)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(callbacks, [])
        self.assertEqual(resource.path.read_bytes(), self.payload.read_bytes())
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        # The pending POSIX activation journal, rather than an anonymous source
        # backup or a success receipt, remains the authority for cold recovery.
        cold = TMEngine(str(resource.path), expected_resource_id=resource.id)
        self.assertEqual(cold.query_exact("old").target, "旧")
        self.assertIsNone(cold.query_exact("incoming"))

    def test_replace_terminal_failure_preserves_new_owner_and_recoverable_receipt(self):
        from platform_fs_posix import _PosixPendingPublication

        resource = self._active_resource(self.repository, "old", "旧")
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.REPLACE_SELECTED,
            destination_resource_id=resource.id,
        )
        original = _PosixPendingPublication._terminal_reproof
        reached = []

        def fail_terminal(pending, retained, preliminary):
            if retained._entry_name == resource.path.name:
                self._assert_locked(resource)
                reached.append(retained._entry_name)
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return original(pending, retained, preliminary)

        with patch.object(_PosixPendingPublication, "_terminal_reproof", new=fail_terminal):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(len(reached), 1)
        self.assertEqual(self.service._ledger.list_receipts(), ())
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        cold = ResourcePortabilityService(ResourceRepository(self.root / "receiver"))
        recovery = cold.inspect_resource_portability_recovery()[0]
        self.assertIs(recovery.disposition, ResourceRecoveryDisposition.COMPLETE_AVAILABLE)
        completed = cold.recover_resource_portability(recovery, ResourceRecoveryAction.COMPLETE)
        self.assertEqual(completed.receipt.owner_generation, 1)
        self.assertEqual(TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact("incoming").target, "新")
        self.assertEqual(cold._ledger.list_pending(), ())


if __name__ == "__main__":
    unittest.main()
