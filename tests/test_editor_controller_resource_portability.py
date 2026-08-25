from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import ResourceKind
from editor_controller import EditorController, EditorControllerError
from resource_package_contracts import (
    ResourceImportMode,
    ResourceRecoveryAction,
    ResourceRecoveryDisposition,
)
from resource_repository import ResourceRepository


_TERMS = b"\xef\xbb\xbfhello,\xe4\xbd\xa0\xe5\xa5\xbd\n"


class EditorControllerResourcePortabilityTests(unittest.TestCase):
    def test_controller_exports_previews_applies_and_reloads_created_resource(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            source = repository.create_resource("Source", ResourceKind.TERMBASE)
            source.path.write_bytes(_TERMS)
            controller = EditorController(repository)
            package = root / "terms.localcat-resource"

            exported = controller.export_resource_package(source.id, package)
            validation = controller.validate_resource_package(package)
            self.assertEqual(exported.receipt.record_count, 1)
            self.assertEqual(validation.record_count, 1)

            preview = controller.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported",
            )
            result = controller.apply_resource_package_import(preview)
            created = repository.get(result.destination_resource_id)
            self.assertEqual(created.path.read_bytes(), _TERMS)
            self.assertIn(created.id, {resource.id for resource in controller.list_resources()})

            with self.assertRaises(EditorControllerError) as replay:
                controller.apply_resource_package_import(preview)
            self.assertEqual(str(replay.exception), "RESOURCE.IMPORT.PREVIEW_STALE")

    def test_runtime_reload_failure_rearms_recoverable_import(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository = ResourceRepository(root / "app")
            source = repository.create_resource("Source", ResourceKind.TERMBASE)
            source.path.write_bytes(_TERMS)
            controller = EditorController(repository)
            package = root / "terms.localcat-resource"
            controller.export_resource_package(source.id, package)
            preview = controller.preview_resource_package_import(
                package,
                ResourceImportMode.CREATE_NEW,
                new_resource_name="Imported",
            )
            with patch.object(
                controller,
                "_reload_resources_after_persisted_mutation",
                side_effect=EditorControllerError("injected reload failure"),
            ):
                with self.assertRaises(EditorControllerError) as caught:
                    controller.apply_resource_package_import(preview)
            self.assertEqual(
                str(caught.exception),
                "RESOURCE.IMPORT.RECOVERY_REQUIRED",
            )
            recovery = controller.inspect_resource_portability_recovery()[0]
            self.assertIs(
                recovery.disposition,
                ResourceRecoveryDisposition.COMPLETE_AVAILABLE,
            )
            completed = controller.recover_resource_portability(
                recovery,
                ResourceRecoveryAction.COMPLETE,
            )
            self.assertIsNotNone(completed.receipt)
            self.assertEqual(controller.inspect_resource_portability_recovery(), ())


if __name__ == "__main__":
    unittest.main()
