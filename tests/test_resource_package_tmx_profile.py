from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from editor_contracts import ResourceConfig, ResourceKind
from resource_package import open_resource_package
from resource_package_contracts import (
    CARRIER_PROFILE_V2,
    MANIFEST_SCHEMA_V2,
    PAYLOAD_PROFILE_SET_V2,
    PortableResourceKind,
    PortableResourceSnapshot,
    ResourceImportMode,
    ResourcePayloadProfile,
    ResourcePortabilityError,
)
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceRepository


_TMX = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<tmx version="1.4"><body><tu><tuv xml:lang="en"><seg>a</seg></tuv>'
    b'<tuv xml:lang="zh-CN"><seg>b</seg></tuv></tu></body></tmx>\n'
)


class _FakeTmxPayloadHandler:
    profile = ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1

    def __init__(
        self,
        *,
        mutate_source: bool = False,
        fail_validation_call: int | None = None,
    ) -> None:
        self.export_calls = 0
        self.validate_calls = 0
        self.reprove_calls = 0
        self.mutate_source = mutate_source
        self.fail_validation_call = fail_validation_call

    def export_snapshot(
        self,
        resource: ResourceConfig,
        destination: Path,
    ) -> PortableResourceSnapshot:
        self.export_calls += 1
        baseline = hashlib.sha256(resource.path.read_bytes()).hexdigest()
        destination.write_bytes(_TMX)
        if self.mutate_source:
            resource.path.write_bytes(resource.path.read_bytes() + b"drift")
        return self._snapshot(baseline)

    def validate_snapshot(self, source: Path) -> PortableResourceSnapshot:
        self.validate_calls += 1
        if self.validate_calls == self.fail_validation_call:
            raise ResourcePortabilityError("TMX.PAYLOAD.INVALID")
        if source.read_bytes() != _TMX:
            raise ResourcePortabilityError("TMX.PAYLOAD.INVALID")
        return self._snapshot(hashlib.sha256(b"cold-validation").hexdigest())

    def reprove_snapshot(
        self,
        resource: ResourceConfig,
        snapshot: PortableResourceSnapshot,
    ) -> None:
        self.reprove_calls += 1
        if (
            hashlib.sha256(resource.path.read_bytes()).hexdigest()
            != snapshot.source_baseline_digest
        ):
            raise ResourcePortabilityError("RESOURCE.EXPORT.SOURCE_STALE")

    @staticmethod
    def _snapshot(baseline: str) -> PortableResourceSnapshot:
        return PortableResourceSnapshot(
            kind=PortableResourceKind.TRANSLATION_MEMORY,
            profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            payload_digest=hashlib.sha256(_TMX).hexdigest(),
            payload_byte_count=len(_TMX),
            record_count=1,
            legacy_record_count=0,
            v1_record_count=0,
            source_baseline_digest=baseline,
            owner_receipt_digest=None,
            owner_generation=3,
            owner_revision=7,
        )


class ResourcePackageTmxProfileTests(unittest.TestCase):
    def _repository(self, root: Path) -> tuple[ResourceRepository, ResourceConfig]:
        repository = ResourceRepository(root / "app")
        resource = repository.create_resource("TM", ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_bytes(b'canonical-owner-bytes\n')
        return repository, resource

    def test_export_cold_validate_publication_and_receipt_use_v2_triple(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._repository(root)
            handler = _FakeTmxPayloadHandler()
            service = ResourcePortabilityService(
                repository,
                tmx_payload_handler=handler,
            )
            first = root / "first.localcat-resource"
            second = root / "second.localcat-resource"
            outcome = service.export_package(
                resource.id,
                first,
                payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )
            service.export_package(
                resource.id,
                second,
                payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with open_resource_package(first) as sealed:
                self.assertEqual(sealed.manifest.schema, MANIFEST_SCHEMA_V2)
                self.assertEqual(sealed.manifest.carrier_profile, CARRIER_PROFILE_V2)
                self.assertEqual(
                    sealed.manifest.payload_profile_set,
                    PAYLOAD_PROFILE_SET_V2,
                )
                self.assertEqual(sealed.manifest.payload.path, "payload/resource.tmx")
                self.assertIs(
                    sealed.manifest.payload_profile,
                    ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                )
            report = service.validate_resource_package(first)
            self.assertEqual(report.payload_digest, hashlib.sha256(_TMX).hexdigest())
            self.assertEqual(report.record_count, 1)
            self.assertIs(
                outcome.receipt.payload_profile,
                ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )
            self.assertEqual(outcome.receipt.record_count, 1)
            self.assertGreaterEqual(handler.validate_calls, 3)
            self.assertEqual(handler.reprove_calls, 2)

    def test_explicit_tmx_requires_handler_and_never_widens_default_v1(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._repository(root)
            destination = root / "tm.localcat-resource"
            destination.write_bytes(b"prior")
            with self.assertRaises(ResourcePortabilityError) as caught:
                ResourcePortabilityService(repository).export_package(
                    resource.id,
                    destination,
                    payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                )
            self.assertEqual(
                caught.exception.code,
                "RESOURCE.PORTABILITY.PAYLOAD_HANDLER_UNAVAILABLE",
            )
            self.assertEqual(destination.read_bytes(), b"prior")

    def test_tmx_package_import_is_rejected_before_any_owner_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_repo, source = self._repository(root / "source")
            package = root / "tm.localcat-resource"
            source_service = ResourcePortabilityService(
                source_repo,
                tmx_payload_handler=_FakeTmxPayloadHandler(),
            )
            source_service.export_package(
                source.id,
                package,
                payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )

            destination_repo, destination = self._repository(root / "destination")
            before = destination.path.read_bytes()
            service = ResourcePortabilityService(
                destination_repo,
                tmx_payload_handler=_FakeTmxPayloadHandler(),
            )
            for mode, destination_id in (
                (ResourceImportMode.CREATE_NEW, None),
                (ResourceImportMode.REPLACE_SELECTED, destination.id),
            ):
                with self.subTest(mode=mode.value):
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        service.preview_resource_package_import(
                            package,
                            mode,
                            destination_resource_id=destination_id,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "RESOURCE.IMPORT.PROFILE_UNSUPPORTED",
                    )
            self.assertEqual(destination.path.read_bytes(), before)
            self.assertEqual(len(destination_repo.list_resources()), 1)

    def test_stale_managed_resource_preserves_prior_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._repository(root)
            destination = root / "tm.localcat-resource"
            destination.write_bytes(b"prior")
            service = ResourcePortabilityService(
                repository,
                tmx_payload_handler=_FakeTmxPayloadHandler(mutate_source=True),
            )
            with self.assertRaises(ResourcePortabilityError) as caught:
                service.export_package(
                    resource.id,
                    destination,
                    payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.SOURCE_STALE")
            self.assertEqual(destination.read_bytes(), b"prior")

    def test_post_publication_cold_validation_failure_restores_prior(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._repository(root)
            destination = root / "tm.localcat-resource"
            destination.write_bytes(b"prior")
            handler = _FakeTmxPayloadHandler(fail_validation_call=3)
            service = ResourcePortabilityService(
                repository,
                tmx_payload_handler=handler,
            )
            with self.assertRaises(ResourcePortabilityError) as caught:
                service.export_package(
                    resource.id,
                    destination,
                    payload_profile=ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
            self.assertEqual(destination.read_bytes(), b"prior")


if __name__ == "__main__":
    unittest.main()
