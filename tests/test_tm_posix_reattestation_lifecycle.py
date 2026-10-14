"""Explicit maintenance consumes one original POSIX family operation."""

from pathlib import Path
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.test_tm_canonical_reattestation import (
    SOURCE_BYTES, _activate_source, _rewrite_persisted_devices,
)
from tm_activation_journal import ActivationPreparationError
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_engine import TMEngine
from tm_migration import MigrationPreflightError, TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator, SQLiteStoreLifecycleError, _activation_journal_path


@unittest.skipIf(sys.platform == "win32", "POSIX device-only maintenance")
class PosixReattestationLifecycleTests(unittest.TestCase):
    def fixture(
        self, root: Path, *, generation: int = 0,
    ) -> tuple[Path, CanonicalResourceIdentity, ResourceStoreCoordinator, TMMigrationService]:
        source = root / "tm.jsonl"
        source.write_bytes(SOURCE_BYTES)
        identity, store_id = _activate_source(source, resource_id="maintenance.test")
        if generation:
            source.write_bytes(SOURCE_BYTES + b'{"source":"new","target":"value"}\n')
            owner = ResourceStoreCoordinator(canonical_store_id=store_id, resource_identity=identity)
            owner.rehydrate_runtime_authority()
            rebuilt = TMMigrationService(resource_identity=identity, canonical_store_id=store_id, coordinator=owner).rebuild_from_snapshot(source, identity.resource_id)
            self.assertIsInstance(rebuilt, MigrationReport)
            assert isinstance(rebuilt, MigrationReport)
            self.assertEqual(rebuilt.activated_generation, generation)
            store_id = rebuilt.canonical_store_id
        journal = _activation_journal_path(identity)
        _rewrite_persisted_devices(journal, persisted_device=source.stat().st_dev + 17)
        owner = ResourceStoreCoordinator(canonical_store_id=store_id, resource_identity=identity)
        service = TMMigrationService(resource_identity=identity, canonical_store_id=store_id, coordinator=owner)
        return source, identity, owner, service

    def test_service_preserves_generation_and_bytes_under_one_family_lock(self):
        for generation in (0, 1):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as temporary:
                source, identity, owner, service = self.fixture(Path(temporary), generation=generation)
                paths = (source, identity.canonical_sidecar_path, identity.snapshot_manifest_path)
                before = {path: (path.stat().st_ino, path.read_bytes()) for path in paths}
                family = owner._family
                self.assertIsNotNone(family)
                assert family is not None
                original_acquire = family._acquire
                calls = []
                def acquire(*args):
                    calls.append(1)
                    return original_acquire(*args)
                with patch.object(family, "_acquire", side_effect=acquire):
                    result = service.reattest_completed_authority(source, identity.resource_id)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.activated_generation, generation)
                self.assertEqual(result.canonical_store_id, owner.canonical_store_id)
                self.assertEqual(before, {path: (path.stat().st_ino, path.read_bytes()) for path in paths})
                self.assertFalse(family._recovery_required)
                self.assertTrue(TMEngine(str(source)).canonical_active)

    def test_classification_is_read_only_and_does_not_clear_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, identity, owner, _service = self.fixture(Path(temporary))
            family = owner._family
            self.assertIsNotNone(family)
            assert family is not None
            family.require_recovery()
            before = {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()}
            self.assertTrue(owner.completed_authority_requires_reattestation())
            self.assertEqual(before, {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()})
            self.assertIsNone(owner.current_generation)
            self.assertTrue(family._recovery_required)

    def test_classification_borrows_existing_operation_and_preserves_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            _source, _identity, owner, _service = self.fixture(Path(temporary))
            with owner._family_transition(recovery=True) as borrow:
                for completed in (False, True):
                    borrow._recovery_completed = completed
                    with patch.object(owner._family, "_acquire", side_effect=AssertionError("nested W1")):
                        self.assertTrue(owner.completed_authority_requires_reattestation(_borrow=borrow))
                    self.assertEqual(borrow._recovery_completed, completed)
                borrow._recovery_completed = False

    def test_pending_owned_close_is_not_executed_by_classification_or_maintenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, identity, owner, service = self.fixture(Path(temporary).resolve())
            family = owner._family
            self.assertIsNotNone(family)
            assert family is not None
            calls = []
            close = lambda: calls.append("unexpected close")
            family._pending_activation_closes.append(close)
            family.require_recovery()
            before = _activation_journal_path(identity).read_bytes()
            self.assertFalse(owner.completed_authority_requires_reattestation())
            with self.assertRaises((ActivationPreparationError, MigrationPreflightError)):
                service.reattest_completed_authority(source, identity.resource_id)
            self.assertEqual(calls, [])
            self.assertEqual(before, _activation_journal_path(identity).read_bytes())
            self.assertEqual(family._pending_activation_closes, [close])
            self.assertTrue(family._recovery_required)

    def test_schema_pending_is_rejected_without_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, identity, owner, service = self.fixture(Path(temporary).resolve())
            pending = source.parent / f".{identity.canonical_sidecar_path.name}.localcat-schema-upgrade.{'a' * 32}.bak.pending"
            pending.write_bytes(b"unresolved schema owner")
            before = {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()}
            self.assertFalse(owner.completed_authority_requires_reattestation())
            with self.assertRaises((ActivationPreparationError, MigrationPreflightError)):
                service.reattest_completed_authority(source, identity.resource_id)
            self.assertEqual(before, {p.name: p.read_bytes() for p in source.parent.iterdir() if p.is_file()})

    def test_failed_projection_does_not_reopen_family_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, identity, owner, service = self.fixture(Path(temporary).resolve())
            family = owner._family
            self.assertIsNotNone(family)
            assert family is not None
            self.assertFalse(family._recovery_required)
            with patch.object(service, "_recovered_reattestation_success_report", side_effect=MigrationPreflightError("MIGRATION.REATTESTATION_RUNTIME_INVALID")):
                with self.assertRaises(MigrationPreflightError):
                    service.reattest_completed_authority(source, identity.resource_id)
            self.assertTrue(family._recovery_required)
            self.assertEqual(owner.current_generation, 0)

    def test_observed_root_replacement_cannot_be_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "resource"
            root.mkdir()
            source, identity, owner, service = self.fixture(root)
            old = root.with_name("original")
            root.rename(old)
            shutil.copytree(old, root)
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            with self.assertRaises(SQLiteStoreLifecycleError) as classified:
                owner.completed_authority_requires_reattestation()
            self.assertEqual(getattr(classified.exception, "code", None), "STORE.RESOURCE_IDENTITY_STALE")
            with self.assertRaisesRegex(MigrationPreflightError, "MIGRATION.REATTESTATION_OWNER_INVALID"):
                service.reattest_completed_authority(source, identity.resource_id)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
            assert owner._family is not None
            self.assertEqual(owner._family._state, "FAILED")


if __name__ == "__main__":
    unittest.main()
