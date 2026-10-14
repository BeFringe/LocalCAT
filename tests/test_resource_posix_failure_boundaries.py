"""Resource 6.2: real owner entry, hostile inputs and observable POSIX drift."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import binascii
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceConfig, ResourceKind
from platform_fs_contracts import BoundDirectoryAuthority, PlatformFileError
from resource_package_contracts import ResourceImportMode, ResourcePortabilityError
from resource_portability import ResourcePortabilityService
from resource_receipt_ledger import ResourcePendingPhase
from resource_repository import ResourceRepository
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_migration import TMMigrationService
from tm_resource_port import TMResourceSnapshotPort
from tm_sqlite_store import ResourceStoreCoordinator
import tm_activation_recovery as recovery
import tm_sqlite_store as store


def files(root: Path) -> dict[str, tuple[int, str]]:
    return {str(p.relative_to(root)): (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


def rewrite_manifest(package: bytes, old: bytes, new: bytes) -> bytes:
    """Recompute both CRCs so malformed semantics reach the logical validator."""
    assert len(old) == len(new)
    changed = bytearray(package)
    size = struct.unpack_from('<I', changed, 18)[0]
    start = 30 + struct.unpack_from('<H', changed, 26)[0]
    manifest = bytes(changed[start:start + size])
    assert old in manifest
    manifest = manifest.replace(old, new, 1)
    changed[start:start + size] = manifest
    crc = binascii.crc32(manifest) & 0xffffffff
    struct.pack_into('<I', changed, 14, crc)
    central = package.index(b'PK\x01\x02')
    struct.pack_into('<I', changed, central + 16, crc)
    return bytes(changed)


@dataclass
class _Fixture:
    root: Path
    package: Path
    package_before: bytes
    repository: ResourceRepository
    service: ResourcePortabilityService


@unittest.skipUnless(os.name == 'posix', 'POSIX resource boundary')
class ResourcePosixFailureBoundariesTests(unittest.TestCase):
    _fixture: _Fixture | None = None

    @property
    def fixture(self) -> _Fixture:
        assert self._fixture is not None
        return self._fixture

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source_repository = ResourceRepository(root / 'source')
        source, _ = self.active(source_repository, 'incoming')
        package = root / 'incoming.localcat-resource'
        ResourcePortabilityService(source_repository).export_package(source.id, package)
        repository = ResourceRepository(root / 'receiver')
        self._fixture = _Fixture(root, package, package.read_bytes(), repository,
                                 ResourcePortabilityService(repository))

    def active(self, repository: ResourceRepository, name: str):
        resource = repository.create_resource(name, ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_text(json.dumps({'source': name, 'target': name + '-target'}) + '\n')
        identity = CanonicalResourceIdentity.from_configured_jsonl(resource.id, resource.path)
        owner = ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=f'store.{resource.id}')
        self.addCleanup(owner.close_family_session)
        outcome = TMMigrationService(resource_identity=identity, canonical_store_id=owner.canonical_store_id,
                                     coordinator=owner, platform_backend=repository.platform_backend).activate_initial(resource.path, resource.id)
        self.assertIs(type(outcome), MigrationReport)
        return resource, owner

    def test_hostile_manifest_payload_and_foreign_carrier_leave_receiver_unchanged(self) -> None:
        mutations = {
            'closed-key': (b'"schema":', b'"xxxxxx":'),
            'traversal': (b'payload/tm.jsonl', b'../evil/tm.jsonl'),
            'profile': (b'localcat-tm-jsonl-v1', b'localcat-tm-jsonl-v9'),
            'digest': (b'"sha256":"', b'"sha256":"'),
            'count': (b'"record_count":1', b'"record_count":2'),
        }
        before = files(self.fixture.repository.config_dir)
        for label, (old, new) in mutations.items():
            with self.subTest(label=label):
                if label == 'digest':
                    start = self.fixture.package_before.index(old) + len(old)
                    old = self.fixture.package_before[start:start + 64]
                    new = b'0' * 64
                invalid = self.fixture.root / (label + '.localcat-resource')
                data = rewrite_manifest(self.fixture.package_before, old, new)
                invalid.write_bytes(data)
                with self.assertRaises(ResourcePortabilityError):
                    self.fixture.service.validate_resource_package(invalid)
                self.assertEqual(invalid.read_bytes(), data)
                self.assertEqual(files(self.fixture.repository.config_dir), before)
        # The logical manifest stays valid; traverse only the local/CD names.
        traversal = bytearray(self.fixture.package_before)
        original_name, hostile_name = b'payload/tm.jsonl', b'../evil/tm.jsonl'
        first_header_name = self.fixture.package_before.index(original_name,
            self.fixture.package_before.index(original_name) + len(original_name))
        central_header_name = self.fixture.package_before.rindex(original_name)
        self.assertNotEqual(first_header_name, central_header_name)
        for offset in (first_header_name, central_header_name):
            traversal[offset:offset + len(original_name)] = hostile_name
        invalid = self.fixture.root / 'carrier-traversal.localcat-resource'
        invalid.write_bytes(traversal)
        with self.assertRaises(ResourcePortabilityError):
            self.fixture.service.validate_resource_package(invalid)
        self.assertEqual(invalid.read_bytes(), bytes(traversal))
        self.assertEqual(files(self.fixture.repository.config_dir), before)
        foreign = self.fixture.root / 'foreign.localcat-resource'
        foreign.write_bytes(b'{"schema":"localcat-project-package-manifest-v1"}\n')
        with self.assertRaises(ResourcePortabilityError):
            self.fixture.service.validate_resource_package(foreign)
        self.assertEqual(files(self.fixture.repository.config_dir), before)
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_before)

    def test_package_links_special_file_and_same_inode_rewrite_are_rejected(self) -> None:
        before = files(self.fixture.repository.config_dir)
        symlink = self.fixture.root / 'symlink.package'
        symlink.symlink_to(self.fixture.package)
        hardlink = self.fixture.root / 'hardlink.package'
        hardlink.hardlink_to(self.fixture.package)
        fifo = self.fixture.root / 'fifo.package'
        os.mkfifo(fifo)
        for path in (symlink, hardlink, fifo):
            with self.subTest(path=path.name), self.assertRaises(ResourcePortabilityError) as caught:
                self.fixture.service.validate_resource_package(path)
            self.assertEqual(caught.exception.code, 'RESOURCE.PACKAGE.SOURCE_UNSAFE')
        hardlink.unlink()
        preview = self.fixture.service.preview_resource_package_import(self.fixture.package, ResourceImportMode.CREATE_NEW)
        inode = self.fixture.package.stat().st_ino
        changed = bytearray(self.fixture.package_before)
        changed[-1] ^= 1
        self.fixture.package.write_bytes(changed)
        with self.assertRaises(ResourcePortabilityError) as caught:
            self.fixture.service.apply_resource_package_import(preview)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.SOURCE_STALE')
        self.assertEqual(self.fixture.package.stat().st_ino, inode)
        self.assertEqual(self.fixture.package.read_bytes(), bytes(changed))
        self.assertEqual(files(self.fixture.repository.config_dir), before)
        self.assertEqual(self.fixture.service._ledger.list_pending(), ())
        self.assertEqual(self.fixture.service._ledger.list_receipts(), ())

    def exercise_drift(self, seam: str, initial: bool) -> None:
        repository = ResourceRepository(self.fixture.root / f'{seam}-{initial}')
        owners: dict[str, ResourceStoreCoordinator] = {}
        resource: ResourceConfig | None = None
        if not initial:
            resource, owner = self.active(repository, 'old')
            owners[resource.id] = owner
        def resolve(selected):
            if selected.id not in owners:
                identity = CanonicalResourceIdentity.from_configured_jsonl(selected.id, selected.path)
                owners[selected.id] = ResourceStoreCoordinator(resource_identity=identity, canonical_store_id=f'store.{selected.id}')
                self.addCleanup(owners[selected.id].close_family_session)
            return owners[selected.id]
        port = TMResourceSnapshotPort(repository.platform_backend, owner_resolver=resolve)
        service = ResourcePortabilityService(repository, tm_port=port)
        preview = service.preview_resource_package_import(self.fixture.package,
            ResourceImportMode.CREATE_NEW if initial else ResourceImportMode.REPLACE_SELECTED,
            destination_resource_id=resource.id if resource is not None else None)
        parent = repository.managed_dir
        held = parent.with_name('held-resources')
        before_registry = repository.registry_path.read_bytes() if repository.registry_path.exists() else None
        before_resources = repository.list_resources()
        observations = []
        callbacks = []
        sql_after_drift = []
        real_build = TMMigrationService._build_stage
        real_publish = BoundDirectoryAuthority.begin_publish
        real_connect = sqlite3.connect
        real_reconnect = store._open_configured_connection
        real_manifest = recovery._publish_activation_manifest
        real_final = TMMigrationService._prove_applied_snapshot
        real_ready = service._mark_pending_receipt_ready
        real_begin = service._ledger.begin
        real_sql_commit = recovery._commit_activation_receipt
        sql_commit_facts = []

        def drift():
            if observations:
                return
            parent.rename(held)
            shutil.copytree(held, parent)
            observations.append((files(parent), files(held)))

        def build(instance, *args, **kwargs):
            if seam == 'builder':
                drift()
            return real_build(instance, *args, **kwargs)

        def publish(bound, candidate, destination, **kwargs):
            if seam == 'source-publication' and any(destination == path.name for path, _ in port._owners.values()):
                drift()
            return real_publish(bound, candidate, destination, **kwargs)

        def begin(*args, **kwargs):
            result = real_begin(*args, **kwargs)
            if seam == 'first-mutation':
                # Core reproof follows owner_prepare, before any source mutation.
                drift()
            return result

        def sql_commit(connection):
            if seam != 'active-sql':
                return real_sql_commit(connection)
            self.assertTrue(connection.in_transaction)
            drift()
            try:
                result = real_sql_commit(connection)
            except BaseException as error:
                sql_commit_facts.append((type(error).__name__, connection.in_transaction))
                raise
            sql_commit_facts.append(('returned', connection.in_transaction))
            return result

        def reconnect(path, **kwargs):
            if seam == 'reconnect' and kwargs.get('_stage_borrow') is not None:
                drift()
            return real_reconnect(path, **kwargs)

        def manifest(*args, **kwargs):
            if seam == 'publication':
                drift()
            return real_manifest(*args, **kwargs)

        def final(instance, *args, **kwargs):
            if seam == 'terminal':
                drift()
            return real_final(instance, *args, **kwargs)

        def ready(receipt):
            callbacks.append(receipt)
            return real_ready(receipt)

        def connect(*args, **kwargs):
            if observations:
                sql_after_drift.append(str(args[0]))
            return real_connect(*args, **kwargs)

        with ExitStack() as stack:
            for target, name, replacement in (
                (TMMigrationService, '_build_stage', build),
                (BoundDirectoryAuthority, 'begin_publish', publish),
                (store, '_open_configured_connection', reconnect),
                (recovery, '_publish_activation_manifest', manifest),
                (TMMigrationService, '_prove_applied_snapshot', final),
                (service, '_mark_pending_receipt_ready', ready),
                (service._ledger, 'begin', begin),
                (recovery, '_commit_activation_receipt', sql_commit),
                (sqlite3, 'connect', connect),
            ):
                stack.enter_context(patch.object(target, name, replacement))
            with self.assertRaises(ResourcePortabilityError) as caught:
                service.apply_resource_package_import(preview)
        self.assertEqual(len(observations), 1, seam)
        self.assertEqual(caught.exception.code, 'RESOURCE.IMPORT.RECOVERY_REQUIRED')
        self.assertEqual(sql_after_drift, [], 'observed drift must stop subsequent SQLite opens')
        if seam == 'active-sql':
            # Unsupported live-transaction experiment: record actual writes and
            # commit/rollback fallout, without promising byte preservation.
            clone_after, original_after = files(parent), files(held)
            clone_before, original_before = observations[0]
            self.assertEqual(len(sql_commit_facts), 1)
            print(json.dumps({'boundary': seam, 'initial': initial,
                'sql_commit': sql_commit_facts,
                'clone_changed': sorted(name for name in set(clone_before) | set(clone_after)
                                        if clone_before.get(name) != clone_after.get(name)),
                'original_changed': sorted(name for name in set(original_before) | set(original_after)
                                           if original_before.get(name) != original_after.get(name)),
                'callback_count': len(callbacks), 'success_receipts': len(service._ledger.list_receipts()),
                'pending': [pending.phase.value for pending in service._ledger.list_pending()]}))
        else:
            self.assertEqual((files(parent), files(held)), observations[0])
        self.assertEqual(service._ledger.list_receipts(), ())
        self.assertEqual(self.fixture.package.read_bytes(), self.fixture.package_before)
        if seam == 'terminal':
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(service._ledger.list_pending()[0].phase, ResourcePendingPhase.RECEIPT_READY)
            self.assertTrue(repository.list_resources())
        else:
            self.assertEqual(callbacks, [])
            self.assertEqual(repository.registry_path.read_bytes() if repository.registry_path.exists() else None, before_registry)
            self.assertEqual(repository.list_resources(), before_resources)
        frozen = (files(parent), files(held))
        selected_id, (_, selected) = next(iter(port._owners.items()))
        if initial:
            self.addCleanup(selected.close_family_session)
        assert selected._family is not None
        self.assertEqual(selected._family._state, 'FAILED')
        with self.assertRaises((ResourcePortabilityError, PlatformFileError)):
            service.preview_resource_package_import(self.fixture.package, ResourceImportMode.REPLACE_SELECTED,
                destination_resource_id=selected_id)
        self.assertEqual((files(parent), files(held)), frozen)

    def test_real_builder_after_same_byte_clone_stops_create_and_replace(self) -> None:
        for initial in (True, False):
            with self.subTest(initial=initial):
                self.exercise_drift('builder', initial)

    def test_observable_first_mutation_reconnect_publication_and_terminal_drift(self) -> None:
        for seam in ('first-mutation', 'source-publication', 'reconnect', 'publication', 'terminal'):
            for initial in (True, False):
                with self.subTest(seam=seam, initial=initial):
                    self.exercise_drift(seam, initial)

    def test_external_replacement_during_sql_commit_records_unknown_and_stops(self) -> None:
        for initial in (True, False):
            with self.subTest(initial=initial):
                self.exercise_drift('active-sql', initial)


if __name__ == '__main__':
    unittest.main()
