"""Resource 6.3: consume the Core-owned private JSONL pair before publication."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from editor_controller import _initial_tm_activation_service
from platform_fs_contracts import BoundDirectoryAuthority, LockLease, ProcessFileLock
from resource_package import open_resource_package
from resource_package_contracts import ResourcePortabilityError
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceRepository
from tests.benchmark_worker_test_support import worker_temporary_directory
from tm_contracts import MigrationReport
from tm_migration import TMMigrationService
from tm_resource_port import TMResourceSnapshotPort


class ResourcePackageJSONLStageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(worker_temporary_directory())).resolve()
        self.repository = ResourceRepository(self.root / "app")
        self.resource = self.repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
        self.resource.path.write_text(json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False) + "\n", encoding="utf-8")
        result = _initial_tm_activation_service(self.resource).activate_initial(self.resource.path, self.resource.id)
        self.assertIs(type(result), MigrationReport)
        self.output = self.root / "output"
        self.output.mkdir()
        self.destination = self.output / "tm.localcat-resource"
        self.service = ResourcePortabilityService(self.repository)
        self.port = self.service._tm
        self.owner = self.port.owner_for_resource(self.resource)
        self.addCleanup(self.owner.close_family_session)

    def export(self):
        return self.service.export_package(self.resource.id, self.destination)

    def stages(self):
        return tuple(path for path in self.output.iterdir() if ".jsonl" in path.name)

    def assert_no_publication(self):
        self.assertEqual(self.destination.read_bytes(), b"old package")
        self.assertEqual(self.service._ledger.list_receipts(), ())
        self.assertEqual(self.service._ledger.list_pending(), ())

    def test_success_consumes_pair_preserves_old_residue_and_matches_direct(self):
        old = self.output / ".historical.jsonl"
        old.write_bytes(b"historical residue")
        direct = self.root / "direct.jsonl"
        with patch.object(TMMigrationService, "prepare_jsonl_export", side_effect=AssertionError("direct must not prepare")):
            direct_result = self.service.export_direct(self.resource.id, direct)
        with patch.object(TMMigrationService, "export_jsonl", side_effect=AssertionError("package must prepare")):
            result = self.export()
        self.assertEqual(self.stages(), (old,))
        self.assertEqual(old.read_bytes(), b"historical residue")
        self.assertTrue(self.port.companion_path(direct).is_file())
        self.assertEqual(result.receipt.payload_digest, direct_result.receipt.payload_digest)
        self.assertEqual(self.service.validate_resource_package(self.destination).record_count, 1)
        with open_resource_package(self.destination, backend=self.repository.platform_backend) as package:
            extracted = self.root / "readback.jsonl"
            package.copy_payload_to(extracted)
            self.assertEqual(extracted.read_bytes(), direct.read_bytes())

    def test_port_projection_programming_error_releases_exact_pair(self):
        primary = RuntimeError("projection programming fault")
        with patch("tm_resource_port.PortableResourceSnapshot", side_effect=primary):
            with self.assertRaises(RuntimeError) as caught:
                self.port.prepare_package_snapshot(self.resource, self.output / "private.jsonl")
        self.assertIs(caught.exception, primary)
        self.assertEqual(self.stages(), ())

    def test_port_digest_and_count_mismatch_release_pair(self):
        for field in ("destination_digest", "exported_count"):
            with self.subTest(field=field):
                prepare = TMMigrationService.prepare_jsonl_export
                def wrong_report(owner, store, destination):
                    token = prepare(owner, store, destination)
                    object.__setattr__(token.report, field, "0" * 64 if field == "destination_digest" else token.report.exported_count + 1)
                    return token
                with patch.object(TMMigrationService, "prepare_jsonl_export", new=wrong_report):
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        self.port.prepare_package_snapshot(self.resource, self.output / "private.jsonl")
                self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
                self.assertEqual(self.stages(), ())

    def test_carrier_and_cold_failure_preserve_primary_and_remove_pair(self):
        for point in ("carrier", "cold"):
            with self.subTest(point=point):
                self.destination.write_bytes(b"old package")
                primary = RuntimeError("normal failed operation")
                target = patch("resource_portability.write_resource_package", side_effect=primary) if point == "carrier" else patch.object(self.service, "validate_resource_package", side_effect=primary)
                with target, self.assertRaises(RuntimeError) as caught:
                    self.export()
                self.assertIs(caught.exception, primary)
                self.assertEqual(self.stages(), ())
                self.assert_no_publication()

    def test_source_reproof_runs_after_release_and_failure_preserves_prior(self):
        self.destination.write_bytes(b"old package")
        primary = ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")
        def reprove(*_args):
            self.assertEqual(self.stages(), ())
            raise primary
        with patch.object(self.service, "_reprove_source", side_effect=reprove):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.export()
        self.assertIs(caught.exception, primary)
        self.assert_no_publication()

    def test_partial_discard_blocks_publication_and_reports_safe_counts(self):
        self.destination.write_bytes(b"old package")
        real_unlink = BoundDirectoryAuthority.unlink_owned
        def unlink(parent, name, expected):
            if name.endswith(".jsonl.tmp.localcat-snapshot.json"):
                raise OSError("fixture private location must not escape")
            return real_unlink(parent, name, expected)
        with patch.object(BoundDirectoryAuthority, "unlink_owned", new=unlink):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.STAGE_FAILED")
        self.assert_no_publication()
        self.assertEqual(len(self.stages()), 1)
        self.assertTrue(self.stages()[0].name.endswith(".localcat-snapshot.json"))
        notes = " ".join(caught.exception.__notes__)
        self.assertIn("removed_count=1", notes)
        self.assertIn("retained_count=1", notes)
        self.assertNotIn(str(self.root), notes)
        self.assertNotIn("private location", notes)

    def test_cleanup_failure_keeps_existing_primary(self):
        self.destination.write_bytes(b"old package")
        primary = RuntimeError("carrier programming fault")
        with patch("resource_portability.write_resource_package", side_effect=primary), patch.object(BoundDirectoryAuthority, "unlink_owned", side_effect=OSError("cleanup")):
            with self.assertRaises(RuntimeError) as caught:
                self.export()
        self.assertIs(caught.exception, primary)
        self.assertIn("RESOURCE.EXPORT.STAGE_FAILED", " ".join(primary.__notes__))
        self.assertIn("retained_count=2", " ".join(primary.__notes__))
        self.assert_no_publication()

    def test_authority_close_failure_blocks_publication_after_pair_removed(self):
        self.destination.write_bytes(b"old package")
        discard = TMMigrationService.discard_prepared_jsonl_export
        close = LockLease.close
        def fail_close(lease):
            close(lease)
            raise OSError("close fault")
        def release(owner, token):
            with patch.object(LockLease, "close", new=fail_close):
                return discard(owner, token)
        with patch.object(TMMigrationService, "discard_prepared_jsonl_export", new=release):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.STAGE_FAILED")
        self.assertIn("EXPORT.STAGE.AUTHORITY_CLOSE_FAILED", " ".join(caught.exception.__notes__))
        self.assertEqual(self.stages(), ())
        self.assert_no_publication()

    def test_unknown_replacement_is_preserved_without_publication(self):
        self.destination.write_bytes(b"old package")
        validate = self.service.validate_resource_package
        def replace_stage(path):
            result = validate(path)
            body = next(path for path in self.stages() if not path.name.endswith(".localcat-snapshot.json"))
            body.unlink()
            body.write_bytes(b"unknown object")
            return result
        with patch.object(self.service, "validate_resource_package", side_effect=replace_stage):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.STAGE_FAILED")
        self.assertEqual(len(self.stages()), 2)
        self.assertIn(b"unknown object", tuple(path.read_bytes() for path in self.stages()))
        self.assert_no_publication()

    def test_prepared_cannot_be_discarded_by_another_port_or_public_facts(self):
        token = self.port.prepare_package_snapshot(self.resource, self.output / "private.jsonl")
        other = TMResourceSnapshotPort(self.repository.platform_backend)
        with self.assertRaises(ValueError):
            other.discard_package_snapshot(token)
        with self.assertRaises(TypeError):
            self.port.discard_package_snapshot(token.snapshot)
        self.assertEqual(len(self.stages()), 2)
        self.port.discard_package_snapshot(token)
        self.port.discard_package_snapshot(token)
        self.assertEqual(self.stages(), ())

    def test_projection_failure_with_cleanup_failure_preserves_primary_and_facts(self):
        primary = RuntimeError("projection fault")
        with patch("tm_resource_port.PortableResourceSnapshot", side_effect=primary), patch.object(BoundDirectoryAuthority, "unlink_owned", side_effect=OSError("cleanup")):
            with self.assertRaises(RuntimeError) as caught:
                self.port.prepare_package_snapshot(self.resource, self.output / "private.jsonl")
        self.assertIs(caught.exception, primary)
        self.assertIn("retained_count=2", " ".join(primary.__notes__))
        self.assertEqual(len(self.stages()), 2)

    def test_core_failure_without_prepared_keeps_existing_pair(self):
        destination = self.output / "private.jsonl"
        self.port.export_snapshot(self.resource, destination)
        before = {path: path.read_bytes() for path in self.stages()}
        with patch.object(TMMigrationService, "discard_prepared_jsonl_export", side_effect=AssertionError("no disposal authority issued")):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self.port.prepare_package_snapshot(self.resource, destination)
        self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.SNAPSHOT_UNAVAILABLE")
        self.assertEqual({path: path.read_bytes() for path in self.stages()}, before)

    def test_cleanup_programming_error_stays_primary_and_reports_terminal_facts(self):
        self.destination.write_bytes(b"old package")
        primary = RuntimeError("cleanup programming fault")
        with patch.object(BoundDirectoryAuthority, "unlink_owned", side_effect=primary):
            with self.assertRaises(RuntimeError) as caught:
                self.export()
        self.assertIs(caught.exception, primary)
        self.assertIn("retained_count=2", " ".join(primary.__notes__))
        self.assertEqual(len(self.stages()), 2)
        self.assert_no_publication()

    def test_peer_export_waiting_for_destination_does_not_block_source_reproof(self):
        import resource_portability
        ready = threading.Event()
        errors = []
        peer_result = []
        acquire = ProcessFileLock.acquire
        writer = resource_portability.write_resource_package
        workers = []
        def observe(backend, parent, name, payload, policy):
            if threading.current_thread().name == "peer-jsonl" and name == ".localcat-tm-export-family.lock":
                ready.set()
            return acquire(backend, parent, name, payload, policy)
        def peer():
            try:
                peer_result.append(self.port.export_snapshot(self.resource, self.output / "peer.jsonl"))
            except BaseException as error:
                errors.append(error)
        def write(*args, **kwargs):
            worker = threading.Thread(target=peer, name="peer-jsonl", daemon=True)
            workers.append(worker)
            worker.start()
            self.assertTrue(ready.wait(10))
            self.assertTrue(worker.is_alive())
            self.assertEqual(peer_result, [])
            return writer(*args, **kwargs)
        try:
            with patch.object(ProcessFileLock, "acquire", new=observe), patch("resource_portability.write_resource_package", side_effect=write):
                result = self.export()
        finally:
            for worker in workers:
                worker.join(10)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(peer_result), 1)
        self.assertEqual(result.receipt.record_count, 1)
        self.assertTrue(self.destination.exists())
