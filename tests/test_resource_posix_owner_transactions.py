from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceConfig, ResourceKind
from platform_fs_contracts import PendingPublication, PlatformFileError, PlatformFileErrorCode
from resource_package_contracts import ResourceImportMode, ResourcePortabilityError
from resource_receipt_ledger import ResourcePendingPhase
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceRepository
from tm_contracts import CanonicalResourceIdentity, MigrationReport, TMRecordDraft
from tm_engine import TMEngine
from tm_migration import TMMigrationService, _InitialActivationResourceReservation
from tm_resource_port import TMResourceSnapshotPort
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteTMStore


def tree_facts(root):
    return {str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_size,
             hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


def exact_target(engine: TMEngine, source: str) -> str:
    match = engine.query_exact(source)
    assert match is not None
    return match.target


def draft(source, target):
    return TMRecordDraft(source_raw=source, target_raw=target, speaker_raw=None,
                        context_prev_raw=None, context_next_raw=None,
                        file_source=None, provenance=())


@dataclass
class _Fixture:
    root: Path
    source_repo: ResourceRepository
    source: ResourceConfig
    package: Path
    package_bytes: bytes
    repository: ResourceRepository
    service: ResourcePortabilityService


@unittest.skipUnless(os.name == 'posix', 'POSIX owner transaction')
class ResourcePosixOwnerTransactionsTests(unittest.TestCase):
    _fixture: _Fixture | None = None

    @property
    def fixture(self) -> _Fixture:
        assert self._fixture is not None, 'unittest setUp must initialize the fixture'
        return self._fixture

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        source_repo = ResourceRepository(root / 'source')
        source, _ = self.active(source_repo, 'incoming', 'new')
        package = root / 'incoming.localcat-resource'
        ResourcePortabilityService(source_repo).export_package(source.id, package)
        repository = ResourceRepository(root / 'receiver')
        self._fixture = _Fixture(root, source_repo, source, package, package.read_bytes(),
                                 repository, ResourcePortabilityService(repository))

    def active(self, repository, name, target):
        resource = repository.create_resource(name, ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_text(json.dumps({'source': name, 'target': target}) + '\n')
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        owner = ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=f'store.{resource.id}')
        result = TMMigrationService(resource_identity=identity, canonical_store_id=owner.canonical_store_id,
                                    coordinator=owner, platform_backend=repository.platform_backend).activate_initial(resource.path, resource.id)
        self.assertIs(type(result), MigrationReport)
        self.addCleanup(owner.close_family_session)
        return resource, owner

    def create_preview(self):
        return self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.CREATE_NEW,
                                                           new_resource_name='received')

    def test_late_legal_append_is_stale_and_preserves_entire_repository(self):
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        store = SQLiteTMStore.from_coordinator(owner)
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        original = self.fixture.service._tm.reprove_destination_baseline
        before = {}
        def append_after_reproof(selected, baseline):
            original(selected, baseline)
            store.append(draft('late legitimate save', 'preserve me'))
            before.update(tree_facts(self.fixture.repository.config_dir))
        with patch.object(self.fixture.service._tm, 'reprove_destination_baseline', append_after_reproof):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.DESTINATION_STALE')
        self.assertEqual(tree_facts(self.fixture.repository.config_dir), before)
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'late legitimate save'), 'preserve me')
        self.assertIsNone(engine.query_exact('incoming'))
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_receipt_commits_once_under_original_operation(self):
        preview = self.create_preview()
        project = TMMigrationService.project_applied_owner
        commit = self.fixture.service._ledger.commit
        families = []
        events = []
        def observe_projection(service, *args, **kwargs):
            facts = project(service, *args, **kwargs)
            families.append(service._coordinator._family)
            return facts
        def observe_commit(receipt):
            events.append(len(families[-1]._operation_tokens))
            return commit(receipt)
        with patch.object(TMMigrationService, 'project_applied_owner', observe_projection), patch.object(self.fixture.service._ledger, 'commit', observe_commit):
            result = self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(events, [1])
        self.assertEqual(self.fixture.service._ledger.get(result.receipt.operation_id), result.receipt)

    def test_injected_owner_survives_replace_export_query_and_append(self):
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        resolved = []
        def resolver(selected):
            self.assertEqual(selected, resource)
            resolved.append(selected.id)
            return owner
        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=resolver)
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('must use injected owner')):
            result = self.fixture.service.apply_resource_package_import(preview)
            self.fixture.service.export_package(resource.id, self.fixture.root / 'roundtrip.localcat-resource')
            port.reopen_snapshot(resource, result.receipt)
        store = SQLiteTMStore.from_coordinator(owner)
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'incoming'), 'new')
        store.append(draft('after apply', 'saved'))
        self.assertEqual(exact_target(engine, 'after apply'), 'saved')
        self.assertEqual(resolved, [resource.id])
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def cold_twice(self, result=None, *, recover=False):
        code = """
import json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from resource_package_contracts import ResourceRecoveryAction
from tm_engine import TMEngine
from tm_sqlite_store import SQLiteTMStore
service = ResourcePortabilityService(ResourceRepository(Path(sys.argv[1])))
if sys.argv[2] == 'recover':
    pending = service.inspect_resource_portability_recovery()
    assert len(pending) == 1
    service.recover_resource_portability(pending[0], ResourceRecoveryAction.COMPLETE)
assert not service._ledger.list_pending()
receipts = service._ledger.list_receipts()
assert len(receipts) == 1
receipt = receipts[0]
resource = service.repository.get(receipt.destination_resource_id)
snapshot = service._tm.reopen_snapshot(resource, receipt)
owner = service._tm.owner_for_resource(resource)
engine = TMEngine.from_open_canonical_store(SQLiteTMStore.from_coordinator(owner),
    configured_jsonl=resource.path, expected_resource_id=resource.id)
assert engine.query_exact('incoming').target == 'new'
assert engine.query_exact('old') is None
print(json.dumps({'digest': snapshot.payload_digest, 'count': snapshot.record_count,
                  'generation': snapshot.owner_generation, 'id': resource.id}))
"""
        observations = []
        for index in range(2):
            child = subprocess.run([sys.executable, '-B', '-c', code,
                                    str(self.fixture.repository.config_dir),
                                    'recover' if recover and index == 0 else 'open'],
                                   cwd=Path(__file__).resolve().parents[1],
                                   text=True, capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            observations.append(json.loads(child.stdout))
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[0]['count'], 1)
        if result is not None:
            self.assertEqual(observations[0]['digest'], result.receipt.payload_digest)
            self.assertEqual(observations[0]['id'], result.destination_resource_id)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_create_registry_ready_final_proof_and_two_cold_processes(self):
        events = []
        original_publish = self.fixture.repository.publish_prepared_create
        original_commit = self.fixture.service._ledger.commit
        def publish(prepared):
            owner = self.fixture.service._tm.owner_for_resource(prepared.resource)
            assert owner._family is not None
            self.assertEqual(len(owner._family._operation_tokens), 1)
            self.assertEqual(self.fixture.repository.list_resources(), ())
            self.assertEqual(self.fixture.service._ledger.list_pending()[0].phase, ResourcePendingPhase.RECEIPT_READY)
            self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
            events.append('registry')
            return original_publish(prepared)
        def commit(receipt):
            resource = self.fixture.repository.get(receipt.destination_resource_id)
            owner = self.fixture.service._tm.owner_for_resource(resource)
            assert owner._family is not None
            self.assertEqual(len(owner._family._operation_tokens), 1)
            events.append('receipt')
            return original_commit(receipt)
        with patch.object(self.fixture.repository, 'publish_prepared_create', publish), patch.object(self.fixture.service._ledger, 'commit', commit):
            result = self.fixture.service.apply_resource_package_import(self.create_preview())
        self.assertEqual(events, ['registry', 'receipt'])
        self.assertNotEqual(result.destination_resource_id, self.fixture.source.id)
        self.cold_twice(result)

    def test_default_owner_is_reused_for_replace_export_and_cold_proof(self):
        resource, _ = self.active(self.fixture.repository, 'old', 'prior')
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        original = self.fixture.service._tm.owner_for_resource(resource)
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('second engine')):
            result = self.fixture.service.apply_resource_package_import(preview)
            self.fixture.service.export_direct(resource.id, self.fixture.root / 'direct.jsonl')
            self.fixture.service._tm.reopen_snapshot(resource, result.receipt)
            self.assertIs(self.fixture.service._tm.owner_for_resource(resource), original)
        # Use only the apply receipt in the independent receiver ledger checks.
        self.assertEqual(len(self.fixture.service._ledger.list_receipts()), 2)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_same_service_can_export_and_preview_after_legacy_activation(self) -> None:
        resource = self.fixture.repository.create_resource('legacy', ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_text('{"source":"legacy","target":"preserved"}\n')
        source_bytes = resource.path.read_bytes()
        destination = self.fixture.root / 'legacy.localcat-resource'
        service = self.fixture.service
        with self.assertRaises(ResourcePortabilityError) as unavailable:
            service.export_package(resource.id, destination)
        self.assertEqual(unavailable.exception.code, 'RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE')
        with self.assertRaises(ResourcePortabilityError):
            service.preview_resource_package_import(
                self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=resource.id)
        self.assertEqual(resource.path.read_bytes(), source_bytes)
        self.assertFalse(destination.exists())
        self.assertEqual(service._ledger.list_receipts(), ())
        self.assertEqual(service._ledger.list_pending(), ())

        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        owner = ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=f'store.{resource.id}')
        self.addCleanup(owner.close_family_session)
        activated = TMMigrationService(
            resource_identity=identity, canonical_store_id=owner.canonical_store_id,
            coordinator=owner, platform_backend=self.fixture.repository.platform_backend,
        ).activate_initial(resource.path, resource.id)
        self.assertIs(type(activated), MigrationReport)
        engine = TMEngine.from_open_canonical_store(
            SQLiteTMStore.from_coordinator(owner), configured_jsonl=resource.path,
            expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'legacy'), 'preserved')

        exported = service.export_package(resource.id, destination)
        self.assertEqual(exported.receipt.record_count, 1)
        self.assertEqual(service.validate_resource_package(destination).record_count, 1)
        retained = service._tm.owner_for_resource(resource)
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('active owner must be retained')):
            preview = service.preview_resource_package_import(
                self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=resource.id)
            self.assertEqual(preview.destination_resource_id, resource.id)
            self.assertIs(service._tm.owner_for_resource(resource), retained)
        self.assertEqual(exact_target(engine, 'legacy'), 'preserved')
        self.assertEqual(service._ledger.list_pending(), ())
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_initial_owner_binding_failure_cannot_be_retried_with_another_owner(self) -> None:
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        _, other_owner = self.active(self.fixture.repository, 'other', 'other target')
        resolved = []
        selected_owner = other_owner

        def resolver(selected: ResourceConfig) -> ResourceStoreCoordinator:
            resolved.append(selected.id)
            return selected_owner

        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=resolver)
        service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        destination = self.fixture.root / 'wrong-binding.localcat-resource'
        with self.assertRaises(ResourcePortabilityError) as initial:
            service.export_package(resource.id, destination)
        self.assertEqual(initial.exception.code, 'RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE')
        selected_owner = owner
        with self.assertRaises(ResourcePortabilityError) as retried:
            service.export_package(resource.id, destination)
        self.assertEqual(retried.exception.code, 'RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE')
        self.assertEqual(resolved, [resource.id])
        self.assertFalse(destination.exists())
        self.assertEqual(service._ledger.list_receipts(), ())
        self.assertEqual(service._ledger.list_pending(), ())
        engine = TMEngine.from_open_canonical_store(
            SQLiteTMStore.from_coordinator(owner), configured_jsonl=resource.path,
            expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'old'), 'prior')

    def test_replace_proves_same_owner_and_two_cold_processes(self):
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=lambda _: owner)
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        apply = TMMigrationService.apply_snapshot_payload
        seen = []
        def observe(service, *args, **kwargs):
            self.assertIs(service._coordinator, owner)
            seen.append(kwargs['expected_destination_baseline'])
            return apply(service, *args, **kwargs)
        with patch.object(TMMigrationService, 'apply_snapshot_payload', observe):
            result = self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result.receipt.owner_generation, 1)
        self.cold_twice(result)

    def test_owner_prepare_rechecks_registry_before_pending_begin(self):
        resource, _ = self.active(self.fixture.repository, 'old', 'prior')
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        apply = TMMigrationService.apply_snapshot_payload
        captured = {}
        def change_registry(service, *args, **kwargs):
            self.fixture.repository.create_resource('concurrent', ResourceKind.TERMBASE)
            captured.update(tree_facts(self.fixture.repository.config_dir))
            return apply(service, *args, **kwargs)
        with patch.object(TMMigrationService, 'apply_snapshot_payload', change_registry), patch.object(self.fixture.service._ledger, 'begin', side_effect=AssertionError('stale wrote pending')):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.PREVIEW_STALE')
        # Private payload staging is temporary; all persistent assets are exact.
        captured = {k: v for k, v in captured.items() if not k.startswith('.resource-package-apply-')}
        self.assertEqual(tree_facts(self.fixture.repository.config_dir), captured)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def terminal_failure(self, boundary):
        preview = self.create_preview()
        ready = self.fixture.service._mark_pending_receipt_ready
        terminal = PendingPublication.terminal_reproof
        close = PendingPublication.close
        receipts = []
        injected = []
        def after_ready(receipt):
            ready(receipt)
            receipts.append(receipt)
        def is_source(pending):
            return bool(receipts and pending.preliminary_facts().content_sha256.hex() == receipts[-1].payload_digest)
        def fail_terminal(pending):
            if boundary == 'terminal' and not injected and is_source(pending):
                injected.append(boundary)
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return terminal(pending)
        def fail_close(pending):
            if boundary == 'close' and not injected and is_source(pending):
                injected.append(boundary)
                close(pending)
                raise OSError('source close failure')
            return close(pending)
        with patch.object(self.fixture.service, '_mark_pending_receipt_ready', after_ready), patch.object(PendingPublication, 'terminal_reproof', fail_terminal), patch.object(PendingPublication, 'close', fail_close):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(injected, [boundary])
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual([p.phase for p in self.fixture.service._ledger.list_pending()], [ResourcePendingPhase.RECEIPT_READY])
        self.cold_twice(recover=True)

    def test_source_terminal_failure_has_no_success_and_cold_recovers(self):
        self.terminal_failure('terminal')

    def test_source_close_failure_has_no_success_and_cold_recovers(self):
        self.terminal_failure('close')

    def test_final_proof_mismatch_keeps_ready_without_success(self):
        from dataclasses import replace
        prove = TMMigrationService._prove_applied_snapshot
        def wrong_proof(service, *args, **kwargs):
            proof = prove(service, *args, **kwargs)
            return replace(proof, canonical_store_id='different-local-authority')
        with patch.object(TMMigrationService, '_prove_applied_snapshot', wrong_proof):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(self.create_preview())
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.service._ledger.list_pending()[0].phase, ResourcePendingPhase.RECEIPT_READY)
        self.cold_twice(recover=True)

    def test_outer_close_failure_preserves_durable_commit_and_returns_failure(self):
        exit_owner = _InitialActivationResourceReservation.__exit__
        injected = []
        def fail_after_release(reservation, *args):
            result = exit_owner(reservation, *args)
            if self.fixture.service._ledger.list_receipts() and not injected:
                injected.append(True)
                raise OSError('outer owner close after committed receipt')
            return result
        with patch.object(_InitialActivationResourceReservation, '__exit__', fail_after_release):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(self.create_preview())
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(injected, [True])
        self.assertEqual(len(self.fixture.service._ledger.list_receipts()), 1)
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())
        self.cold_twice()

    def test_completed_owner_recovery_uses_runtime_hydration_after_append(self):
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=lambda _: owner)
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        result = self.fixture.service.apply_resource_package_import(self.fixture.service.preview_resource_package_import(
            self.fixture.package, ResourceImportMode.REPLACE_SELECTED, destination_resource_id=resource.id))
        store = SQLiteTMStore.from_coordinator(owner)
        store.append(draft('later', 'kept'))
        hydrate = owner.rehydrate_runtime_authority
        with patch.object(owner, 'recover_durable_activation', side_effect=AssertionError('historical activation is not runtime hydration')), patch.object(owner, 'rehydrate_runtime_authority', wraps=hydrate) as observed:
            # The historical receipt no longer describes current revision/count,
            # but runtime rehydration must preserve the legitimate append.
            with self.assertRaises(ResourcePortabilityError) as caught:
                port.reopen_snapshot(resource, result.receipt)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.COLD_REOPEN_FAILED')
        self.assertEqual(observed.call_count, 1)
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'later'), 'kept')
        self.assertIs(port.owner_for_resource(resource), owner)

    def test_source_inode_stale_is_stable_and_preserves_receiver(self):
        preview = self.create_preview()
        before = tree_facts(self.fixture.repository.config_dir)
        replacement = self.fixture.root / 'replacement.localcat-resource'
        replacement.write_bytes(self.fixture.package_bytes)
        replacement.replace(self.fixture.package)
        with self.assertRaises(ResourcePortabilityError) as caught:
            self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.SOURCE_STALE')
        self.assertEqual(tree_facts(self.fixture.repository.config_dir), before)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_final_registry_drift_cannot_commit_receipt(self):
        prove = TMMigrationService._prove_applied_snapshot
        def registry_changed(service, *args, **kwargs):
            proof = prove(service, *args, **kwargs)
            other = ResourceRepository(self.fixture.repository.config_dir)
            other.create_resource('concurrent', ResourceKind.TERMBASE)
            return proof
        with patch.object(TMMigrationService, '_prove_applied_snapshot', registry_changed):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(self.create_preview())
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.service._ledger.list_pending()[0].phase, ResourcePendingPhase.RECEIPT_READY)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_final_package_drift_cannot_commit_receipt(self):
        prove = TMMigrationService._prove_applied_snapshot
        def package_changed(service, *args, **kwargs):
            proof = prove(service, *args, **kwargs)
            replacement = self.fixture.root / 'replaced.localcat-resource'
            replacement.write_bytes(self.fixture.package_bytes)
            replacement.replace(self.fixture.package)
            return proof
        with patch.object(TMMigrationService, '_prove_applied_snapshot', package_changed):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(self.create_preview())
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.service._ledger.list_pending()[0].phase, ResourcePendingPhase.RECEIPT_READY)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def test_runtime_close_failure_is_recovered_by_same_owner_after_append(self):
        import sqlite3
        from tm_sqlite_store import SQLiteStoreLifecycleError
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=lambda _: owner)
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        result = self.fixture.service.apply_resource_package_import(self.fixture.service.preview_resource_package_import(
            self.fixture.package, ResourceImportMode.REPLACE_SELECTED, destination_resource_id=resource.id))
        store = SQLiteTMStore.from_coordinator(owner)
        store.append(draft('after append', 'keep'))
        connect = sqlite3.connect
        connections = []
        closes = []
        class RetainedConnection(sqlite3.Connection):
            def close(self):
                closes.append(self)
                if self is connections[0] and closes.count(self) == 1:
                    raise OSError('retained completed runtime SQL close')
                return super().close()
        def retained_connect(*args, **kwargs):
            kwargs['factory'] = RetainedConnection
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch.object(sqlite3, 'connect', retained_connect):
            with self.assertRaises(ResourcePortabilityError) as failed:
                port.reopen_snapshot(resource, result.receipt)
        self.assertEqual(failed.exception.code, 'RESOURCE.IMPORT.COLD_REOPEN_FAILED')
        self.assertTrue(connections)
        self.assertEqual(closes.count(connections[0]), 1)
        with self.assertRaises(SQLiteStoreLifecycleError):
            store.canonical_revision()
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('failed export must not rebind')):
            with self.assertRaises(ResourcePortabilityError) as exported:
                port.export_snapshot(resource, self.fixture.root / 'unavailable.jsonl')
        self.assertEqual(exported.exception.code, 'RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE')
        self.assertFalse((self.fixture.root / 'unavailable.jsonl').exists())
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('must not rebind failed owner')), patch.object(owner, 'recover_durable_activation', side_effect=AssertionError('wrong recovery entry')):
            with self.assertRaises(ResourcePortabilityError) as historical:
                port.reopen_snapshot(resource, result.receipt)
        self.assertEqual(historical.exception.code, 'RESOURCE.IMPORT.COLD_REOPEN_FAILED')
        self.assertEqual(closes.count(connections[0]), 2)
        self.assertIs(port.owner_for_resource(resource), owner)
        self.assertEqual(store.canonical_revision().record_count, 2)
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'after append'), 'keep')

    def test_source_naming_failure_returns_prior_without_completing_armed_receipt(self):
        from platform_fs_posix import _PosixRootedDirectory
        from resource_package_contracts import ResourceRecoveryDisposition
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=TMResourceSnapshotPort(
            self.fixture.repository.platform_backend, owner_resolver=lambda _: owner))
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                               destination_resource_id=resource.id)
        before = resource.path.stat().st_ino, resource.path.read_bytes()
        rename = _PosixRootedDirectory._rename_candidate
        hits = []
        def fail_after_naming(parent, candidate, destination, **kwargs):
            result = rename(parent, candidate, destination, **kwargs)
            if destination == resource.path.name and not hits:
                hits.append(destination)
                raise PlatformFileError(PlatformFileErrorCode.RECOVERY_REQUIRED, retryable=True)
            return result
        with patch.object(_PosixRootedDirectory, '_rename_candidate', fail_after_naming):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(hits, [resource.path.name])
        self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), before)
        store = SQLiteTMStore.from_coordinator(owner)
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'old'), 'prior')
        self.assertIsNone(engine.query_exact('incoming'))
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertIsNone(self.fixture.service._ledger.list_pending()[0].receipt.owner_generation)
        with patch.object(owner, 'rehydrate_runtime_authority', wraps=owner.rehydrate_runtime_authority) as hydration:
            recovery = self.fixture.service.inspect_resource_portability_recovery()[0]
        self.assertIs(recovery.disposition, ResourceRecoveryDisposition.MANUAL_REQUIRED)
        self.assertEqual(hydration.call_count, 0, 'ARMED does not authorize canonical recovery')
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)
        code = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from resource_package_contracts import ResourceRecoveryAction, ResourceRecoveryDisposition, ResourcePortabilityError
from tm_sqlite_store import ResourceStoreCoordinator
service = ResourcePortabilityService(ResourceRepository(Path(sys.argv[1])))
before = service._ledger.list_pending()
with patch.object(ResourceStoreCoordinator, 'rehydrate_runtime_authority', side_effect=AssertionError('unpublished template entered Core recovery')) as hydrate:
    recovery = service.inspect_resource_portability_recovery()[0]
    assert recovery.disposition is ResourceRecoveryDisposition.MANUAL_REQUIRED
    try:
        service.recover_resource_portability(recovery, ResourceRecoveryAction.COMPLETE)
    except ResourcePortabilityError as error:
        assert error.code == 'RESOURCE.RECOVERY.DECISION_INVALID'
    else:
        raise AssertionError('unpublished template committed')
    assert hydrate.call_count == 0
assert service._ledger.list_pending() == before
assert service._ledger.list_receipts() == ()
print(json.dumps({'operation': before[0].receipt.operation_id, 'phase': before[0].phase.value}))
"""
        observed = []
        for _ in range(2):
            child = subprocess.run([sys.executable, '-B', '-c', code, str(self.fixture.repository.config_dir)],
                                   cwd=Path(__file__).resolve().parents[1],
                                   text=True, capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            observed.append(json.loads(child.stdout))
        self.assertEqual(observed, [{'operation': preview.operation_id, 'phase': 'armed'}] * 2)

    def stopped_replacement(self, *, outer_close=False):
        from platform_fs_contracts import LockLease
        from tm_sqlite_store import SQLiteStoreLifecycleError
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=TMResourceSnapshotPort(
            self.fixture.repository.platform_backend, owner_resolver=lambda _: owner))
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                                     destination_resource_id=resource.id)
        before = resource.path.stat().st_ino, resource.path.read_bytes()
        real_close = LockLease.close
        hits = []
        def fail_close(lease):
            real_close(lease)
            hits.append(True)
            raise OSError('outer close after public outcome was prepared')
        def unfinished(owner, facts):
            hits.append(True)
            raise OSError('source returned but owner finalization interrupted')
        apply = TMMigrationService.apply_snapshot_payload
        def closing(migration, *args, **kwargs):
            with patch.object(LockLease, 'close', fail_close):
                return apply(migration, *args, **kwargs)
        failure = (patch.object(TMMigrationService, 'apply_snapshot_payload', closing) if outer_close else
                   patch.object(ResourceStoreCoordinator, '_finish_snapshot_source_return', unfinished))
        with patch.object(ResourceStoreCoordinator, 'publish_activation', side_effect=OSError('ordinary publication failure')), failure:
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertTrue(hits)
        self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), before)
        with self.assertRaises(SQLiteStoreLifecycleError):
            SQLiteTMStore.from_coordinator(owner).canonical_revision()
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)
        self.assertEqual(self.fixture.repository.get(resource.id), resource)
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual([p.phase for p in self.fixture.service._ledger.list_pending()], [ResourcePendingPhase.ARMED])
        return resource, preview

    def assert_pending_cannot_be_abandoned(self, resource, preview):
        from resource_package_contracts import ResourceRecoveryAction, ResourceRecoveryDisposition
        pending = self.fixture.service._ledger.list_pending()
        for action in (ResourceRecoveryAction.ROLLBACK, ResourceRecoveryAction.COMPLETE):
            recovery = self.fixture.service.inspect_resource_portability_recovery()[0]
            self.assertIs(recovery.disposition, ResourceRecoveryDisposition.MANUAL_REQUIRED)
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.recover_resource_portability(recovery, action)
            self.assertEqual(caught.exception.code, 'RESOURCE.RECOVERY.DECISION_INVALID')
        self.assertEqual(self.fixture.service._ledger.list_pending(), pending)
        code = """
import json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from resource_package_contracts import ResourceRecoveryAction, ResourceRecoveryDisposition, ResourcePortabilityError
from tm_engine import TMEngine
service = ResourcePortabilityService(ResourceRepository(Path(sys.argv[1])))
pending = service._ledger.list_pending()
assert len(pending) == 1 and pending[0].phase.value == 'armed'
assert pending[0].receipt.operation_id == sys.argv[3]
resource = service.repository.get(sys.argv[2])
# Core can cold-recover the current canonical. This supplies no missing
# historical Resource/outer-operation completion evidence.
engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
assert engine.query_exact('old').target == 'prior'
assert engine.query_exact('incoming') is None
for action in (ResourceRecoveryAction.ROLLBACK, ResourceRecoveryAction.COMPLETE):
    recovery = service.inspect_resource_portability_recovery()[0]
    assert recovery.disposition is ResourceRecoveryDisposition.MANUAL_REQUIRED
    try:
        service.recover_resource_portability(recovery, action)
    except ResourcePortabilityError as error:
        assert error.code == 'RESOURCE.RECOVERY.DECISION_INVALID'
    else:
        raise AssertionError('incomplete Resource operation abandoned')
assert service._ledger.list_pending() == pending
assert service._ledger.list_receipts() == ()
print(json.dumps({'operation': pending[0].receipt.operation_id, 'phase': 'armed', 'core_query': 'prior'}))
"""
        for _ in range(2):
            child = subprocess.run([sys.executable, '-B', '-c', code, str(self.fixture.repository.config_dir), resource.id, preview.operation_id],
                                   cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {'operation': preview.operation_id, 'phase': 'armed', 'core_query': 'prior'})

    def test_returned_source_without_owner_finalization_cannot_abandon_pending(self):
        self.assert_pending_cannot_be_abandoned(*self.stopped_replacement())

    def test_outer_close_failure_cannot_consume_private_completed_outcome(self):
        self.assert_pending_cannot_be_abandoned(*self.stopped_replacement(outer_close=True))

    def test_replacement_failure_requires_preservation_bound_to_this_call(self):
        from dataclasses import replace
        from tm_contracts import AssetKind, AssetPreservationEvidence, AssetPreservationState, MigrationFailure, RecoveryLocator
        for changed in ('source_before', 'active_generation', 'source_unverified', 'store_unverified'):
            with self.subTest(changed=changed):
                resource, owner = self.active(self.fixture.repository, changed, 'prior')
                service = ResourcePortabilityService(self.fixture.repository, tm_port=TMResourceSnapshotPort(
                    self.fixture.repository.platform_backend, owner_resolver=lambda _: owner))
                preview = service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                                  destination_resource_id=resource.id)
                apply = TMMigrationService.apply_snapshot_payload
                observed = []
                def incomplete(migration, *args, **kwargs):
                    outcome = apply(migration, *args, **kwargs)
                    self.assertIs(type(outcome), MigrationFailure)
                    assert type(outcome) is MigrationFailure
                    observed.append(outcome)
                    if changed == 'source_before':
                        wrong = preview.validation.payload_digest
                        return replace(outcome, original_source_preservation=AssetPreservationEvidence(
                            AssetKind.ORIGINAL_SOURCE, AssetPreservationState.VERIFIED_UNCHANGED, wrong, wrong))
                    if changed == 'active_generation':
                        assert outcome.active_generation is not None
                        return replace(outcome, active_generation=outcome.active_generation + 1)
                    source = changed == 'source_unverified'
                    evidence = outcome.original_source_preservation if source else outcome.active_store_preservation
                    assert evidence.before_digest is not None
                    changed_evidence = replace(evidence, state=AssetPreservationState.UNVERIFIED, observed_digest=None)
                    fields = {'original_source_preservation' if source else 'active_store_preservation': changed_evidence}
                    return replace(outcome, **fields, recovery_locators=(RecoveryLocator(
                        resource.path, evidence.asset_kind, evidence.before_digest),), retryable=False)
                with patch.object(ResourceStoreCoordinator, 'publish_activation', side_effect=OSError('ordinary publication failure')), patch.object(TMMigrationService, 'apply_snapshot_payload', incomplete):
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        service.apply_resource_package_import(preview)
                self.assertEqual(len(observed), 1)
                self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
                self.assertEqual(service._ledger.get_pending(preview.operation_id).phase, ResourcePendingPhase.ARMED)
                self.assertFalse(any(r.operation_id == preview.operation_id for r in service._ledger.list_receipts()))
                self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)

    def ordinary_publication_failure(self, boundary):
        from tm_contracts import AssetPreservationState, MigrationFailure
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        port = TMResourceSnapshotPort(self.fixture.repository.platform_backend, owner_resolver=lambda _: owner)
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=port)
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                                                                     destination_resource_id=resource.id)
        store = SQLiteTMStore.from_coordinator(owner)
        before_revision = store.canonical_revision()
        before = resource.path.stat().st_ino, resource.path.read_bytes()
        apply = TMMigrationService.apply_snapshot_payload
        seen = []
        def observe(migration, *args, **kwargs):
            outcome = apply(migration, *args, **kwargs)
            seen.append(outcome)
            return outcome
        with patch.object(ResourceStoreCoordinator, boundary, side_effect=OSError('ordinary publication failure')), patch.object(TMMigrationService, 'apply_snapshot_payload', observe):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.APPLY_FAILED')
        self.assertEqual(len(seen), 1)
        outcome = seen[0]
        self.assertIs(type(outcome), MigrationFailure)
        self.assertFalse(outcome.canonical_authority_published or outcome.canonical_authority_ambiguous)
        self.assertEqual(outcome.recovery_locators, ())
        self.assertEqual(outcome.original_source_preservation.state, AssetPreservationState.VERIFIED_UNCHANGED)
        self.assertEqual(outcome.original_source_preservation.before_digest, hashlib.sha256(before[1]).hexdigest())
        self.assertEqual(outcome.active_store_preservation.state, AssetPreservationState.VERIFIED_UNCHANGED)
        self.assertEqual(outcome.active_generation, before_revision.generation)
        self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), before)
        self.assertEqual(store.canonical_revision(), before_revision)
        self.assertEqual(self.fixture.repository.get(resource.id), resource)
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)
        # The bound owner remains usable for the normal query/update/export
        # path after a closed ordinary failure; no new engine is acquired.
        engine = TMEngine.from_open_canonical_store(store, configured_jsonl=resource.path, expected_resource_id=resource.id)
        self.assertEqual(exact_target(engine, 'old'), 'prior')
        self.assertIsNone(engine.query_exact('incoming'))
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('ordinary failure rebound owner')):
            store.append(draft('later', 'keep'))
            self.fixture.service.export_package(resource.id, self.fixture.root / 'after-failure.localcat-resource')
        self.assertIs(port.owner_for_resource(resource), owner)
        code = """
import json, sys
from pathlib import Path
from resource_repository import ResourceRepository
from resource_portability import ResourcePortabilityService
from tm_engine import TMEngine
repository = ResourceRepository(Path(sys.argv[1]))
resource = repository.get(sys.argv[2])
engine = TMEngine(str(resource.path), expected_resource_id=resource.id)
assert engine.query_exact('old').target == 'prior'
assert engine.query_exact('later').target == 'keep'
assert engine.query_exact('incoming') is None
service = ResourcePortabilityService(repository)
assert service._ledger.list_pending() == ()
assert service.inspect_resource_portability_recovery() == ()
print(json.dumps({'generation': engine.canonical_store.canonical_revision().generation, 'count': 2}))
"""
        for _ in range(2):
            child = subprocess.run([sys.executable, '-B', '-c', code, str(self.fixture.repository.config_dir), resource.id],
                                   cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {'generation': before_revision.generation, 'count': 2})
        with patch('tm_resource_port.TMEngine', side_effect=AssertionError('retry rebound owner')):
            retry = self.fixture.service.preview_resource_package_import(
                self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=resource.id)
            result = self.fixture.service.apply_resource_package_import(retry)
            port.reopen_snapshot(resource, result.receipt)
        self.assertIs(port.owner_for_resource(resource), owner)
        self.assertEqual(exact_target(engine, 'incoming'), 'new')
        self.assertIsNone(engine.query_exact('old'))
        self.assertEqual(result.receipt.owner_generation, before_revision.generation + 1)
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())

    def test_prepared_publication_failure_closes_and_cold_reopens_twice(self):
        self.ordinary_publication_failure('publish_prepared_activation')

    def test_activation_publication_failure_closes_and_cold_reopens_twice(self):
        self.ordinary_publication_failure('publish_activation')

    def test_initial_ordinary_failure_uses_absent_prior_store_contract(self):
        from tm_contracts import AssetPreservationState, MigrationFailure
        preview = self.create_preview()
        apply = TMMigrationService.apply_snapshot_payload
        seen = []
        def observe(migration, *args, **kwargs):
            outcome = apply(migration, *args, **kwargs)
            seen.append(outcome)
            return outcome
        with patch.object(ResourceStoreCoordinator, 'publish_activation', side_effect=OSError('initial publication failure')), patch.object(TMMigrationService, 'apply_snapshot_payload', observe):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.APPLY_FAILED')
        self.assertEqual(len(seen), 1)
        outcome = seen[0]
        self.assertIs(type(outcome), MigrationFailure)
        self.assertIsNone(outcome.active_generation)
        self.assertEqual(outcome.active_store_preservation.state, AssetPreservationState.NOT_APPLICABLE)
        self.assertEqual(outcome.original_source_preservation.before_digest, preview.validation.payload_digest)
        self.assertEqual(self.fixture.repository.list_resources(), ())
        self.assertEqual(tuple(self.fixture.repository.managed_dir.glob('*.jsonl')), ())
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)
        # New identity creation still works after the failed attempt.
        result = self.fixture.service.apply_resource_package_import(self.create_preview())
        self.cold_twice(result)

    def test_unknown_source_return_failure_retains_pending_and_original_exception(self):
        from resource_package_contracts import ResourceRecoveryDisposition
        resource, owner = self.active(self.fixture.repository, 'old', 'prior')
        self.fixture.service = ResourcePortabilityService(self.fixture.repository, tm_port=TMResourceSnapshotPort(
            self.fixture.repository.platform_backend, owner_resolver=lambda _: owner))
        preview = self.fixture.service.preview_resource_package_import(
            self.fixture.package, ResourceImportMode.REPLACE_SELECTED, destination_resource_id=resource.id)
        before = resource.path.stat().st_ino, resource.path.read_bytes()
        unexpected = RuntimeError('unexpected source return finalization failure')
        with patch.object(ResourceStoreCoordinator, 'publish_activation', side_effect=OSError('ordinary publication failure')), patch.object(ResourceStoreCoordinator, '_finish_snapshot_source_return', side_effect=unexpected) as finish:
            with self.assertRaises(Exception) as caught:
                self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(finish.call_count, 1)
        self.assertEqual((resource.path.stat().st_ino, resource.path.read_bytes()), before)
        pending = self.fixture.service._ledger.list_pending()
        self.assertEqual([p.phase for p in pending], [ResourcePendingPhase.ARMED])
        self.assertIs(type(caught.exception), ResourcePortabilityError)
        assert type(caught.exception) is ResourcePortabilityError
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertIs(caught.exception.__cause__, unexpected)
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.repository.get(resource.id), resource)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_bytes)
        cold = ResourcePortabilityService(ResourceRepository(self.fixture.repository.config_dir))
        recovery = cold.inspect_resource_portability_recovery()[0]
        self.assertIs(recovery.disposition, ResourceRecoveryDisposition.MANUAL_REQUIRED)
        self.assertEqual(cold._ledger.list_pending(), pending)


if __name__ == '__main__':
    unittest.main()
