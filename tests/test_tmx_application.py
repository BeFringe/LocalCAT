from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from editor_contracts import ResourceKind
from editor_controller import EditorController, _initial_tm_activation_service
from resource_package import open_resource_package
from resource_package_contracts import (
    CARRIER_PROFILE_V2,
    ResourcePayloadProfile,
)
from resource_repository import ResourceRepository
from tm_contracts import MigrationReport
from project_workspace_contracts import SegmentIdentity
from qt_editor import (
    _compose_chunk_controller,
    _compose_editor_controller,
    _compose_tmx_export_service,
)
from tests.test_multi_document_cluster4_qt import _export_package, _write_json
from tmx_application import TmxApplicationError, TmxExportApplicationService
from tmx_context_interchange import inspect_tmx_payload


class TmxApplicationTests(unittest.TestCase):
    def _canonical_resource(self, root: Path):
        repository = ResourceRepository(root / "app")
        resource = repository.create_resource("Canonical", ResourceKind.TRANSLATION_MEMORY)
        resource.path.write_text(
            "".join(
                (
                    json.dumps(
                        {"source": "Hello", "target": "你好"},
                        ensure_ascii=False,
                    )
                    + "\n",
                    json.dumps(
                        {"source": "Goodbye", "target": "再见"},
                        ensure_ascii=False,
                    )
                    + "\n",
                )
            ),
            encoding="utf-8",
        )
        outcome = _initial_tm_activation_service(resource).activate_initial(
            resource.path,
            resource.id,
        )
        self.assertIs(type(outcome), MigrationReport)
        return repository, resource

    def test_managed_resource_direct_preview_publish_and_cold_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._canonical_resource(root)
            controller = EditorController(repository)
            service = TmxExportApplicationService(controller, repository)
            destination = root / "canonical.tmx"

            preparation = service.prepare_resource_export(
                resource.id,
                "en",
                "zh-CN",
                destination,
            )

            self.assertFalse(destination.exists())
            self.assertEqual(preparation.preview.included_count, 2)
            receipt = service.publish(preparation)
            proof = inspect_tmx_payload(destination)
            self.assertTrue(receipt.durable)
            self.assertEqual(receipt.after_digest, proof.payload_digest)
            self.assertEqual(proof.included_count, 2)
            with self.assertRaises(TmxApplicationError):
                service.publish(preparation)

    def test_controller_exports_real_tmx_resourcepackage_v2(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            repository, resource = self._canonical_resource(root)
            controller = EditorController(repository)
            destination = root / "canonical.localcat-resource"

            outcome = controller.export_tmx_resource_package(
                resource.id,
                destination,
                "en",
                "zh-CN",
            )

            self.assertTrue(outcome.destination_preserved)
            with open_resource_package(destination) as sealed:
                self.assertEqual(sealed.manifest.carrier_profile, CARRIER_PROFILE_V2)
                self.assertIs(
                    sealed.manifest.payload_profile,
                    ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
                )
                payload = (root / "cold-payload.tmx").resolve()
                sealed.copy_payload_to(payload)
                proof = inspect_tmx_payload(payload)
                self.assertEqual(proof.included_count, 2)
            report = controller.validate_resource_package(destination)
            self.assertIs(
                report.payload_profile,
                ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
            )

    def test_real_workspace_and_explicit_chunk_export_keep_distinct_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            sources = root / "sources"
            first = sources / "a.json"
            second = sources / "b.json"
            _write_json(
                first,
                name="A",
                segments=(
                    ("a-1", "A one", "甲一", True),
                    ("a-2", "A two", "", False),
                ),
            )
            _write_json(
                second,
                name="B",
                segments=(
                    ("b-1", "B one", "乙一", True),
                    ("b-2", "B two", "", False),
                ),
            )
            package = root / "workspace.localcat-project"
            _export_package(sources, (first, second), package, name="Workspace TMX")
            repository = ResourceRepository(root / "app")
            controller, _composition = _compose_editor_controller(repository)
            chunk_controller = _compose_chunk_controller(controller, repository)
            controller.open_project_package(package)
            identities = tuple(
                SegmentIdentity(
                    item.identity.document.document_id,
                    item.identity.local_segment_id,
                )
                for item in controller.workspace_view.segments[:2]
            )
            chunk_controller.apply_mutation(
                chunk_controller.preview_create_chunk("First chapter", identities)
            )
            chunk_id = chunk_controller.project_view().chunks[0].chunk_id
            service = _compose_tmx_export_service(
                controller,
                repository,
                chunk_controller,
            )

            project_destination = root / "project.tmx"
            project = service.prepare_project_export(
                "project",
                "en",
                "zh-CN",
                project_destination,
            )
            service.publish(project)
            chunk_destination = root / "chunk.tmx"
            chunk = service.prepare_project_export(
                f"chunk:{chunk_id}",
                "en",
                "zh-CN",
                chunk_destination,
            )
            service.publish(chunk)

            project_proof = inspect_tmx_payload(project_destination)
            chunk_proof = inspect_tmx_payload(chunk_destination)
            self.assertEqual(project.preview.document_count, 2)
            self.assertEqual(project.preview.included_count, 2)
            self.assertEqual(project.preview.excluded_count, 2)
            self.assertEqual(chunk.preview.document_count, 1)
            self.assertEqual(chunk.preview.included_count, 1)
            self.assertEqual(chunk.preview.excluded_count, 1)
            self.assertEqual(project_proof.included_count, 2)
            self.assertEqual(chunk_proof.included_count, 1)
            self.assertIn(
                (f"chunk:{chunk_id}", "First chapter"),
                service.available_project_scopes(),
            )


if __name__ == "__main__":
    unittest.main()
