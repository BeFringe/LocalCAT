"""TMX bytes enter the carrier without an export-side private payload file."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from editor_controller import _initial_tm_activation_service
from resource_package_contracts import ResourcePayloadProfile, ResourcePortabilityError
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceRepository
from tm_contracts import MigrationReport
from tmx_context_contracts import TmxEffectiveLocales
from tmx_resource_package_handler import TmxResourcePackagePayloadHandler


class ResourcePackagePayloadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = ResourceRepository(self.root / "app")
        self.resource = self.repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
        self.resource.path.write_text(json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False) + "\n", encoding="utf-8")
        outcome = _initial_tm_activation_service(self.resource).activate_initial(self.resource.path, self.resource.id)
        self.assertIs(type(outcome), MigrationReport)
        self.output = self.root / "exports"
        self.output.mkdir()
        self.handler = TmxResourcePackagePayloadHandler(TmxEffectiveLocales("en", "zh-CN"))
        self.service = ResourcePortabilityService(self.repository, tmx_payload_handler=self.handler)

    def _export(self):
        return self.service.export_package(self.resource.id, self.output / "tm.localcat-resource", payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1)

    def test_export_never_creates_a_private_tmx_payload_file(self):
        from platform_fs_contracts import BoundDirectoryAuthority
        real_create = BoundDirectoryAuthority.create_candidate
        names = []
        def create(parent, name, **kwargs):
            names.append(name)
            self.assertFalse(".tmx" in name)
            return real_create(parent, name, **kwargs)
        with patch.object(BoundDirectoryAuthority, "create_candidate", new=create):
            self._export()
        self.assertTrue(names)
        self.assertFalse(any(".tmx" in path.name or ".jsonl" in path.name for path in self.output.iterdir()))
        self.assertEqual(self.service.validate_resource_package(self.output / "tm.localcat-resource").record_count, 1)

    def test_prepare_returns_exact_bytes_without_output_files(self):
        snapshot, payload = self.handler.prepare_export_payload(self.resource)
        self.assertIs(type(payload), bytes)
        self.assertEqual(len(payload), snapshot.payload_byte_count)
        self.assertEqual(tuple(self.output.iterdir()), ())

    def test_independent_parser_failure_precedes_final_publication(self):
        destination = self.output / "tm.localcat-resource"
        destination.write_bytes(b"prior package")
        with patch("tmx_resource_package_handler.inspect_tmx_payload", side_effect=ValueError("cold parser fault")) as parser, patch.object(type(self.service._artifact_save), "publish") as publish:
            with self.assertRaises(ResourcePortabilityError):
                self._export()
        parser.assert_called_once()
        publish.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"prior package")

    def test_parser_metadata_drift_is_rejected_against_prepared_proof(self):
        import tmx_context_interchange as interchange

        collect = interchange._collect_parser_cold_facts
        for changed_field in ("content_digest", "prop_count"):
            with self.subTest(changed_field=changed_field):
                def drift(*args):
                    facts = collect(*args)
                    return replace(facts, **{
                        changed_field: "0" * 64 if changed_field == "content_digest" else facts.prop_count + 1,
                    })

                with patch.object(interchange, "_collect_parser_cold_facts", side_effect=drift), patch.object(type(self.service._artifact_save), "publish") as publish:
                    with self.assertRaises(ResourcePortabilityError):
                        self._export()
                publish.assert_not_called()
                self.assertFalse((self.output / "tm.localcat-resource").exists())

    def test_carrier_failure_preserves_primary_and_prior_destination(self):
        destination = self.output / "tm.localcat-resource"
        destination.write_bytes(b"prior package")
        primary = OSError("fixture carrier fault")
        with patch("resource_portability.write_resource_package_bytes", side_effect=primary):
            with self.assertRaises(OSError) as caught:
                self._export()
        self.assertIs(caught.exception, primary)
        self.assertEqual(destination.read_bytes(), b"prior package")

    def test_mutable_payload_is_rejected_without_publication(self):
        real_prepare = self.handler.prepare_export_payload
        def mutable(resource):
            snapshot, payload = real_prepare(resource)
            return snapshot, bytearray(payload)
        with patch.object(TmxResourcePackagePayloadHandler, "prepare_export_payload", side_effect=mutable), patch.object(type(self.service._artifact_save), "publish") as publish:
            with self.assertRaises(TypeError):
                self._export()
        publish.assert_not_called()

    def test_candidate_cleanup_cannot_mask_primary_validation_failure(self):
        primary = ResourcePortabilityError("RESOURCE.EXPORT.RECOVERY_REQUIRED")
        with patch.object(ResourcePortabilityService, "validate_resource_package", side_effect=primary), patch("resource_portability._unlink_snapshot", side_effect=OSError("cleanup fault")):
            with self.assertRaises(ResourcePortabilityError) as caught:
                self._export()
        self.assertIs(caught.exception, primary)


class ImmutableCarrierPayloadTests(unittest.TestCase):
    def test_bytes_writer_preserves_exact_existing_zip_format(self):
        from resource_package import write_resource_package, write_resource_package_bytes
        from tests import test_resource_package_carrier as fixture
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            payload = b'{"source":"a","target":"b"}\n'
            manifest = fixture.ResourcePackageCarrierTests()._manifest(payload)
            source = root / "payload.jsonl"
            source.write_bytes(payload)
            first, second = root / "file.package", root / "bytes.package"
            write_resource_package(first, manifest, source)
            write_resource_package_bytes(second, manifest, payload)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            for invalid in (bytearray(payload), memoryview(payload)):
                with self.assertRaises(TypeError):
                    write_resource_package_bytes(root / "invalid.package", manifest, invalid)
            with self.assertRaises(ResourcePortabilityError):
                write_resource_package_bytes(root / "mismatch.package", manifest, payload + b"x")
            self.assertFalse((root / "invalid.package").exists())
            self.assertFalse((root / "mismatch.package").exists())

    def test_carrier_close_fault_does_not_mask_primary_write_failure(self):
        from platform_fs_contracts import CandidateFile
        from resource_package import write_resource_package_bytes
        from tests import test_resource_package_carrier as fixture
        with tempfile.TemporaryDirectory() as raw:
            primary = OSError("primary write failure")
            real_close = CandidateFile.close
            def close(candidate):
                real_close(candidate)
                raise OSError("cleanup close failure")
            with patch.object(CandidateFile, "write_chunks", side_effect=primary), patch.object(CandidateFile, "close", new=close):
                with self.assertRaises(OSError) as caught:
                    write_resource_package_bytes(Path(raw).resolve() / "failed.package", fixture.ResourcePackageCarrierTests()._manifest(b"{}"), b"{}")
            self.assertIs(caught.exception, primary)
