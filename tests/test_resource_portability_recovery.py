from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from resource_package_contracts import (
    ResourceImportMode,
    ResourcePortabilityError,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
)
from resource_portability import ResourcePortabilityService
from resource_repository import ResourceError, ResourceRepository


_MIXED_TERMS = (
    b"\xef\xbb\xbfsource,target\n"
    b"localcat-term-v1,id-1,Case,Target,true,false\n"
)


class ResourcePortabilityRecoveryTests(unittest.TestCase):
    def test_cold_service_completes_owner_published_create_after_registry_fault(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_repository = ResourceRepository(root / "source-app")
            source = source_repository.create_resource(
                "Source terms",
                ResourceKind.TERMBASE,
            )
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repository).export_package(
                source.id,
                package,
            )

            destination_root = root / "destination-app"
            destination_repository = ResourceRepository(destination_root)
            service = ResourcePortabilityService(destination_repository)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Recovered terms",
            )
            with patch.object(
                destination_repository,
                "publish_prepared_create",
                side_effect=ResourceError("injected registry failure"),
            ):
                with self.assertRaises(ResourcePortabilityError) as caught:
                    service.apply_resource_package_import(preview)
            self.assertEqual(
                caught.exception.code,
                "RESOURCE.IMPORT.RECOVERY_REQUIRED",
            )
            self.assertEqual(destination_repository.list_resources(), ())

            cold_repository = ResourceRepository(destination_root)
            cold_service = ResourcePortabilityService(cold_repository)
            recoveries = cold_service.inspect_resource_portability_recovery()
            self.assertEqual(len(recoveries), 1)
            recovery = recoveries[0]
            self.assertIs(
                recovery.disposition,
                ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
            )

            outcome = cold_service.recover_resource_portability(
                recovery,
                ResourceRecoveryAction.COMPLETE,
            )
            self.assertEqual(outcome.receipt.operation_id, recovery.operation_id)
            recovered = cold_repository.get(outcome.receipt.destination_resource_id)
            self.assertEqual(recovered.name, "Recovered terms")
            self.assertEqual(recovered.path.read_bytes(), _MIXED_TERMS)
            self.assertEqual(cold_service._ledger.list_pending(), ())
            self.assertEqual(
                cold_service._ledger.get(recovery.operation_id),
                outcome.receipt,
            )

    def test_recovery_preview_is_single_use_and_reproves_current_pending_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source_repository = ResourceRepository(root / "source-app")
            source = source_repository.create_resource(
                "Source terms",
                ResourceKind.TERMBASE,
            )
            source.path.write_bytes(_MIXED_TERMS)
            package = root / "terms.localcat-resource"
            ResourcePortabilityService(source_repository).export_package(
                source.id,
                package,
            )

            destination_root = root / "destination-app"
            destination_repository = ResourceRepository(destination_root)
            service = ResourcePortabilityService(destination_repository)
            preview = service.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
            )
            with patch.object(
                destination_repository,
                "publish_prepared_create",
                side_effect=ResourceError("injected registry failure"),
            ):
                with self.assertRaises(ResourcePortabilityError):
                    service.apply_resource_package_import(preview)

            cold = ResourcePortabilityService(ResourceRepository(destination_root))
            recovery = cold.inspect_resource_portability_recovery()[0]
            pending = cold._ledger.get_pending(recovery.operation_id)
            destination = (
                cold.repository.managed_dir / pending.destination_relative_path
            )
            destination.write_bytes(b"unknown bytes")
            with self.assertRaises(ResourcePortabilityError) as stale:
                cold.recover_resource_portability(
                    recovery,
                    ResourceRecoveryAction.COMPLETE,
                )
            self.assertEqual(
                stale.exception.code,
                "RESOURCE.RECOVERY.PREVIEW_STALE",
            )
            self.assertEqual(cold.repository.list_resources(), ())


if __name__ == "__main__":
    unittest.main()
