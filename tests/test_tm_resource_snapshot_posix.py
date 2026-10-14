from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceConfig, ResourceKind
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from resource_package_contracts import (
    ResourceExportOutcome, ResourceImportMode, ResourcePortabilityError,
    ResourceRecoveryAction, ResourceRecoveryDisposition, receipt_to_bytes,
)
from resource_portability import ResourcePortabilityService
from resource_receipt_ledger import ResourcePendingPhase
from resource_repository import ResourceError, ResourceRepository
from tm_contracts import AssetKind, CanonicalResourceIdentity, MigrationReport, RecoveryLocator, SourceBindingState
from tm_engine import TMEngine
from tm_migration import (
    TMMigrationService,
    _InitialActivationReservationError,
    _InitialActivationResourceReservation,
)
from tm_resource_port import TMResourceSnapshotPort
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteStoreLifecycleError, SQLiteTMStore


@unittest.skipUnless(os.name == "posix", "POSIX snapshot owner transaction")
class TMResourceSnapshotPosixTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temporary: tempfile.TemporaryDirectory[str]
        self.root: Path
        self.source_repository: ResourceRepository
        self.source: ResourceConfig
        self.package: Path
        self.exported: ResourceExportOutcome
        self.package_before: bytes
        self.payload: Path
        self.repository: ResourceRepository
        self.service: ResourcePortabilityService

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

    def _active_resource(self, repository: ResourceRepository, source: str, target: str) -> ResourceConfig:
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

    def _assert_exact_target(self, resource: ResourceConfig, source: str, target: str) -> None:
        match = TMEngine(str(resource.path), expected_resource_id=resource.id).query_exact(source)
        assert match is not None
        self.assertEqual(match.target, target)

    def _assert_cold(self, result, generation):
        cold_repository = ResourceRepository(self.root / "receiver")
        resource = cold_repository.get(result.destination_resource_id)
        cold = ResourcePortabilityService(cold_repository)
        facts = cold._tm.reopen_snapshot(resource, result.receipt)
        self.assertEqual(facts.owner_generation, generation)
        self.assertEqual(facts.record_count, 1)
        engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
        incoming = engine.query_exact("incoming")
        assert incoming is not None
        self.assertEqual(incoming.target, "新")
        self.assertIsNone(engine.query_exact("old"))
        self.assertIn(result.receipt, cold._ledger.list_receipts())
        self.assertEqual(cold._ledger.list_pending(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)
        self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
        return resource

    def _assert_prior_in_two_cold_processes(self, resource, generation):
        code = """
import json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from tm_engine import TMEngine
repository = ResourceRepository(Path(sys.argv[1]))
resource = repository.get(sys.argv[2])
engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
assert engine.query_exact('old').target == '旧'
assert engine.query_exact('incoming') is None
service = ResourcePortabilityService(repository)
assert service._ledger.list_pending() == ()
assert service._ledger.list_receipts() == ()
assert service.inspect_resource_portability_recovery() == ()
print(json.dumps({'generation': engine.canonical_store.canonical_revision().generation,
                  'source_inode': resource.path.stat().st_ino}))
"""
        for _ in range(2):
            child = subprocess.run(
                [sys.executable, '-B', '-c', code, str(self.repository.config_dir), resource.id],
                cwd=Path(__file__).resolve().parents[1], text=True,
                capture_output=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {
                'generation': generation, 'source_inode': resource.path.stat().st_ino,
            })

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
        assert old_store is not None
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
        new_store = TMEngine(str(resource.path), expected_resource_id=resource.id).canonical_store
        assert new_store is not None
        self.assertNotEqual(old_store.coordinator.canonical_store_id, new_store.coordinator.canonical_store_id)
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
        self._assert_exact_target(resource, "old", "旧")

    def test_source_publication_failure_cleans_candidates_under_w1(self):
        self._source_publication_failure(fail_return=False)

    def test_source_return_failure_preserves_exact_prior_backup_under_w1(self):
        self._source_publication_failure(fail_return=True)

    def _source_publication_failure(self, *, fail_return):
        import tm_migration
        from platform_fs_posix import _PosixRootedDirectory

        resource = self._active_resource(self.repository, "old", "旧")
        before = resource.path.read_bytes()
        before_inode = resource.path.stat().st_ino
        port = TMResourceSnapshotPort(self.repository.platform_backend)
        owner = port.owner_for_resource(resource)
        prior_revision = SQLiteTMStore.from_coordinator(owner).canonical_revision()
        validated = port.validate_snapshot(self.payload)
        original = _PosixRootedDirectory._rename_candidate
        original_cleanup = tm_migration._cleanup_bound_export_artifacts
        reached = []
        cleanup_observed = []
        callbacks = []

        def fail_source(parent, candidate, destination, **kwargs):
            if destination == resource.path.name:
                self._assert_locked(resource)
                candidate_path = resource.path.parent / candidate._name
                role = "return" if ".localcat-import-recovery-" in candidate._name else "forward"
                reached.append((role, candidate_path.stat().st_ino, candidate_path.read_bytes()))
                if role == "forward" or fail_return:
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
            with self.assertRaises(ResourcePortabilityError) as caught:
                port.replace_snapshot(resource, self.payload, validated, owner_commit=callbacks.append)
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual([event[0] for event in reached], ["forward", "return"])
        self.assertEqual(reached[0][2], self.payload.read_bytes())
        self.assertNotEqual(reached[0][1], before_inode)
        self.assertEqual(reached[1], ("return", before_inode, before))
        self.assertTrue(cleanup_observed)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.repository.get(resource.id), resource)
        self.assertEqual(self.service._ledger.list_receipts(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)
        remaining = tuple(resource.path.parent.glob("*.localcat-import-*"))
        if fail_return:
            self.assertFalse(resource.path.exists())
            self.assertEqual(len(remaining), 1)
            self.assertIn(".localcat-import-recovery-", remaining[0].name)
            self.assertEqual((remaining[0].stat().st_ino, remaining[0].read_bytes()), (before_inode, before))
            with self.assertRaises(SQLiteStoreLifecycleError):
                SQLiteTMStore.from_coordinator(owner).canonical_revision()
        else:
            self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), (before_inode, before))
            self.assertEqual(remaining, ())
            self.assertEqual(SQLiteTMStore.from_coordinator(owner).canonical_revision(), prior_revision)
            self._assert_prior_in_two_cold_processes(resource, prior_revision.generation)

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
        self._assert_exact_target(resource, "old", "旧")

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
        self._assert_exact_target(resource, "old", "旧")
        self.assertEqual(self.service._ledger.list_receipts(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)

    def test_activation_failure_respects_prior_owner_preservation_proof(self):
        import tm_activation_recovery as recovery

        for initial in (True, False):
            with self.subTest(initial=initial):
                prepared = None
                before_inode = None
                owner = None
                if initial:
                    prepared = self.repository.prepare_resource_create("failed", ResourceKind.TRANSLATION_MEMORY)
                    resource = prepared.resource
                    before = None
                else:
                    resource = self._active_resource(self.repository, "old", "旧")
                    before = resource.path.read_bytes()
                    before_inode = resource.path.stat().st_ino
                port = TMResourceSnapshotPort(self.repository.platform_backend)
                validated = port.validate_snapshot(self.payload)
                callbacks = []
                injected = []
                cancelled = []
                real_cancel = ResourceStoreCoordinator.cancel_prepared_activation
                prior_revision = None
                if not initial:
                    owner = port.owner_for_resource(resource)
                    prior_revision = SQLiteTMStore.from_coordinator(owner).canonical_revision()

                def fail_activation(coordinator, *args, **kwargs):
                    self._assert_locked(resource)
                    injected.append(coordinator.durable_activation_phase)
                    raise OSError("injected activation failure")

                def cancel(coordinator, *args, **kwargs):
                    result = real_cancel(coordinator, *args, **kwargs)
                    cancelled.append(coordinator)
                    return result

                with (
                    patch.object(ResourceStoreCoordinator, "publish_prepared_activation", new=fail_activation),
                    patch.object(ResourceStoreCoordinator, "cancel_prepared_activation", new=cancel),
                    patch.object(recovery, "_replace_activation_database", wraps=recovery._replace_activation_database) as naming,
                ):
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        method = port.create_snapshot if initial else port.replace_snapshot
                        method(resource, self.payload, validated, owner_commit=callbacks.append)
                self.assertEqual(injected, [None if initial else "GENERATION_PUBLISHED"])
                self.assertEqual(len(cancelled), 1, "the real cancellation must finish")
                self.assertEqual(naming.call_count, 0, "this attempt never named a DB")
                self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.APPLY_FAILED")
                self.assertEqual(callbacks, [])
                self.assertFalse(tuple(resource.path.parent.glob("*.localcat-import-*")))
                self.assertEqual(self.service._ledger.list_receipts(), ())
                self.assertEqual(self.service._ledger.list_pending(), ())
                self.assertEqual(self.package.read_bytes(), self.package_before)
                if initial:
                    self.assertFalse(resource.path.exists())
                    assert prepared is not None
                    self.repository.cancel_prepared_create(prepared, remove_owned_file=False)
                else:
                    # A previous completed journal is not this attempt's DB
                    # publication. The real cancellation proved the full prior.
                    assert owner is not None and prior_revision is not None and before_inode is not None
                    self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), (before_inode, before))
                    self.assertEqual(SQLiteTMStore.from_coordinator(owner).canonical_revision(), prior_revision)
                    self.assertEqual(self.repository.get(resource.id), resource)
                    self._assert_exact_target(resource, "old", "旧")
                    self._assert_prior_in_two_cold_processes(resource, prior_revision.generation)

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
        self._assert_exact_target(resource, "old", "旧")

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
        assert completed.receipt is not None and completed.receipt.destination_resource_id is not None
        resource = cold.repository.get(completed.receipt.destination_resource_id)
        self._assert_exact_target(resource, "incoming", "新")
        self.assertEqual(cold._ledger.list_pending(), ())
        self.assertEqual(self.package.read_bytes(), self.package_before)

    def test_unproven_replacement_rollback_leaves_source_for_owner_recovery(self):
        import tm_activation_recovery as recovery

        resource = self._active_resource(self.repository, "old", "旧")
        before = (resource.path.stat().st_ino, resource.path.read_bytes())
        preview = self.service.preview_resource_package_import(
            self.package, ResourceImportMode.REPLACE_SELECTED,
            destination_resource_id=resource.id,
        )
        owner = self.service._tm.owner_for_resource(resource)
        db = owner.active_store_path
        assert db is not None
        db_before = (db.stat().st_ino, db.read_bytes())
        real_cancel = ResourceStoreCoordinator.cancel_prepared_activation
        real_ready = self.service._mark_pending_receipt_ready
        injected = []
        cancellation_errors = []
        callbacks = []

        def fail_receipt(*args, **kwargs):
            self._assert_locked(resource)
            # The real publisher has consumed/named the stage and persisted
            # DB_REPLACED. An exception at publish_activation entry cannot
            # exercise this state, and its unused rollback mock proved nothing.
            self.assertEqual(owner.durable_activation_phase, "DB_REPLACED")
            self.assertNotEqual((db.stat().st_ino, db.read_bytes()), db_before)
            injected.append("DB_REPLACED")
            raise OSError("receipt fault after real DB naming")

        def cancel(coordinator, *args, **kwargs):
            try:
                return real_cancel(coordinator, *args, **kwargs)
            except Exception as error:
                cancellation_errors.append(str(error))
                raise

        def ready(receipt):
            callbacks.append(receipt)
            return real_ready(receipt)

        with (
            patch.object(recovery, "_publish_activation_receipt", new=fail_receipt),
            patch.object(ResourceStoreCoordinator, "cancel_prepared_activation", new=cancel),
            patch.object(self.service, "_mark_pending_receipt_ready", new=ready),
        ):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.service.apply_resource_package_import(preview)
        self.assertEqual(injected, ["DB_REPLACED"])
        self.assertEqual(cancellation_errors, ["ACTIVATION.RECOVERY_MISMATCH"])
        self.assertEqual(caught.exception.code, "RESOURCE.IMPORT.RECOVERY_REQUIRED")
        self.assertEqual(callbacks, [])
        self.assertEqual(hashlib.sha256(resource.path.read_bytes()).hexdigest(), preview.validation.payload_digest)
        backups = tuple(resource.path.parent.glob("*.localcat-import-recovery-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0].stat().st_ino, backups[0].read_bytes()), before)
        self.assertEqual(self.repository.get(resource.id), resource)
        self.assertEqual(self.service._ledger.list_receipts(), ())
        pending = self.service._ledger.list_pending()
        self.assertEqual([p.phase for p in pending], [ResourcePendingPhase.ARMED])
        with self.assertRaises(SQLiteStoreLifecycleError):
            SQLiteTMStore.from_coordinator(owner).canonical_revision()
        self.assertEqual(self.package.read_bytes(), self.package_before)
        code = """
import hashlib, json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from resource_package_contracts import ResourceRecoveryDisposition
from resource_receipt_ledger import ResourcePendingPhase
from tm_engine import TMEngine
repository = ResourceRepository(Path(sys.argv[1]))
resource = repository.get(sys.argv[2])
# Public open must be the first recovery consumer in each fresh process.
engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
store = engine.canonical_store
assert store is not None
incoming = engine.query_exact('incoming')
assert incoming is not None and incoming.target == '新'
assert engine.query_exact('old') is None
assert store.canonical_revision().generation == 1
service = ResourcePortabilityService(repository)
pending = service._ledger.list_pending()
assert len(pending) == 1 and pending[0].phase is ResourcePendingPhase.ARMED
preview = service.inspect_resource_portability_recovery()[0]
assert preview.disposition is ResourceRecoveryDisposition.MANUAL_REQUIRED
assert service._ledger.list_pending() == pending
assert service._ledger.list_receipts() == ()
backups = tuple(resource.path.parent.glob('*.localcat-import-recovery-*'))
assert len(backups) == 1
print(json.dumps({'operation': pending[0].receipt.operation_id, 'generation': 1,
 'prior_inode': backups[0].stat().st_ino,
 'prior_sha256': hashlib.sha256(backups[0].read_bytes()).hexdigest()}))
"""
        for _ in range(2):
            child = subprocess.run(
                [sys.executable, '-B', '-c', code, str(self.repository.config_dir), resource.id],
                cwd=Path(__file__).resolve().parents[1], text=True,
                capture_output=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {
                'operation': preview.operation_id,
                'generation': 1,
                'prior_inode': before[0], 'prior_sha256': hashlib.sha256(before[1]).hexdigest(),
            })

    def test_replace_terminal_failure_preserves_new_owner_and_recoverable_receipt(self):
        self._replace_terminal_failure(diverged=False)

    def test_diverged_source_terminal_failure_preserves_exact_prior_backup(self):
        self._replace_terminal_failure(diverged=True)

    def _replace_terminal_failure(self, *, diverged: bool) -> None:
        from platform_fs_posix import _PosixPendingPublication

        resource = self._active_resource(self.repository, "old", "旧")
        canonical_source_digest = hashlib.sha256(resource.path.read_bytes()).hexdigest()
        if diverged:
            resource.path.write_text('{"source":"external-source","target":"分歧原文"}\n', encoding="utf-8")
        before_stat = resource.path.stat()
        before = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size,
                  hashlib.sha256(resource.path.read_bytes()).hexdigest())
        prior_engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
        prior_store = prior_engine.canonical_store
        assert prior_store is not None
        self.assertEqual(prior_store.source_binding_monitor.observe().state is SourceBindingState.SOURCE_DIVERGED,
                         diverged)
        self.assertEqual(before[3] != canonical_source_digest, diverged)
        prior_match = prior_engine.query_exact("old")
        assert prior_match is not None
        self.assertEqual(prior_match.target, "旧")
        prior_store_id = prior_store.coordinator.canonical_store_id
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
        pending = self.service._ledger.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIs(pending[0].phase, ResourcePendingPhase.RECEIPT_READY)
        self.assertEqual(pending[0].receipt.operation_id, preview.operation_id)
        self.assertEqual(pending[0].receipt.owner_generation, 1)
        self.assertEqual(self.repository.get(resource.id), resource)

        # The live error locates this exact prior object. The cold service has no
        # persisted locator and must not infer cleanup ownership by scanning.
        cause: BaseException | None = caught.exception
        locators: list[RecoveryLocator] = []
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            raw_locators = getattr(cause, "recovery_locators", ())
            if raw_locators:
                assert isinstance(raw_locators, tuple)
                locators.clear()
                for locator in raw_locators:
                    assert isinstance(locator, RecoveryLocator)
                    locators.append(locator)
            cause = cause.__cause__
        self.assertEqual(len(locators), 1)
        locator = locators[0]
        self.assertIs(locator.asset_kind, AssetKind.ORIGINAL_SOURCE)
        self.assertEqual(locator.expected_digest, before[3])
        backup = locator.path
        backup_stat = backup.stat()
        self.assertEqual((backup_stat.st_dev, backup_stat.st_ino, backup_stat.st_size,
                          hashlib.sha256(backup.read_bytes()).hexdigest()), before)
        self.assertEqual(self.package.read_bytes(), self.package_before)
        code = """
import hashlib, json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from resource_package_contracts import ResourceRecoveryAction, ResourceRecoveryDisposition, receipt_to_bytes
from resource_receipt_ledger import ResourcePendingPhase
from tm_engine import TMEngine
repository = ResourceRepository(Path(sys.argv[1]))
resource = repository.get(sys.argv[2])
service = ResourcePortabilityService(repository)
expected_receipt = bytes.fromhex(sys.argv[4])
previews = service.inspect_resource_portability_recovery()
if sys.argv[5] == 'first':
    assert len(previews) == 1
    assert previews[0].disposition is ResourceRecoveryDisposition.COMPLETE_AVAILABLE
    pending = service._ledger.list_pending()
    assert len(pending) == 1 and pending[0].phase is ResourcePendingPhase.RECEIPT_READY
    assert receipt_to_bytes(pending[0].receipt) == expected_receipt
    assert service._ledger.list_receipts() == ()
    completed = service.recover_resource_portability(previews[0], ResourceRecoveryAction.COMPLETE)
    assert completed.receipt is not None and receipt_to_bytes(completed.receipt) == expected_receipt
else:
    assert previews == ()
engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
store = engine.canonical_store
assert store is not None
assert store.canonical_revision().generation == 1
incoming = engine.query_exact('incoming')
assert incoming is not None and incoming.target == '新'
assert engine.query_exact('old') is None
receipts = service._ledger.list_receipts()
assert len(receipts) == 1 and receipt_to_bytes(receipts[0]) == expected_receipt
facts = service._tm.reopen_snapshot(resource, receipts[0])
assert facts.owner_generation == 1 and facts.record_count == 1
assert store.coordinator.canonical_store_id != sys.argv[6]
assert service._ledger.list_pending() == ()
assert service.inspect_resource_portability_recovery() == ()
assert repository.get(resource.id) == resource
# Observation only: the backup path is never passed into the recovery service.
backup = Path(sys.argv[3])
stat = backup.stat()
print(json.dumps({'backup': [stat.st_dev, stat.st_ino, stat.st_size,
    hashlib.sha256(backup.read_bytes()).hexdigest()], 'generation': facts.owner_generation,
    'operation': receipts[0].operation_id, 'receipts': len(receipts)}))
"""
        for phase in ("first", "second"):
            child = subprocess.run(
                [sys.executable, "-B", "-c", code, str(self.repository.config_dir), resource.id,
                 str(backup), receipt_to_bytes(pending[0].receipt).hex(), phase, prior_store_id],
                cwd=Path(__file__).resolve().parents[1], text=True,
                capture_output=True, timeout=30,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {
                "backup": list(before), "generation": 1,
                "operation": preview.operation_id, "receipts": 1,
            })
        self.assertEqual(self.package.read_bytes(), self.package_before)


if __name__ == "__main__":
    unittest.main()
