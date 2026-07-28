from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from editor_contracts import ResourceKind
from resource_repository import ResourceError, ResourceRepository


class ResourceRepositoryTest(unittest.TestCase):
    def _make_defaults(self, root: Path) -> tuple[Path, Path]:
        tm_path = root / "tm.jsonl"
        termbase_path = root / "terms.csv"
        tm_path.write_text("", encoding="utf-8")
        termbase_path.write_text("Glossary,术语表\n", encoding="utf-8-sig")
        return tm_path, termbase_path

    def test_bootstraps_existing_default_resources_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tm_path, termbase_path = self._make_defaults(root)
            config_dir = root / "app-data"

            first = ResourceRepository(config_dir, tm_path, termbase_path)
            second = ResourceRepository(config_dir, tm_path, termbase_path)

            resources = second.list_resources()
        self.assertEqual(len(resources), 2)
        self.assertEqual(
            {resource.kind for resource in resources},
            {ResourceKind.TRANSLATION_MEMORY, ResourceKind.TERMBASE},
        )
        self.assertTrue(all(resource.active and resource.lookup and resource.update for resource in resources))
        self.assertEqual(len(first.list_resources()), 2)

    def test_creates_resources_only_under_managed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ResourceRepository(root / "app-data")

            tm = repository.create_resource("Client Alpha", ResourceKind.TRANSLATION_MEMORY)
            terms = repository.create_resource("术语 表", ResourceKind.TERMBASE)

            managed_dir = (root / "app-data" / "resources").resolve()
            self.assertTrue(tm.path.is_relative_to(managed_dir))
            self.assertTrue(terms.path.is_relative_to(managed_dir))
            self.assertEqual(tm.path.suffix, ".jsonl")
            self.assertEqual(terms.path.suffix, ".csv")
            self.assertEqual(tm.path.read_text(encoding="utf-8"), "")
            self.assertEqual(terms.path.read_text(encoding="utf-8-sig"), "")

    def test_create_normalizes_supported_serialized_resource_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResourceRepository(Path(temp_dir) / "app-data")

            tm = repository.create_resource(
                "Serialized TM",
                ResourceKind.TRANSLATION_MEMORY.value,
            )
            terms = repository.create_resource(
                "Serialized terms",
                ResourceKind.TERMBASE.value,
            )

            self.assertIs(tm.kind, ResourceKind.TRANSLATION_MEMORY)
            self.assertEqual(tm.path.suffix, ".jsonl")
            self.assertIs(terms.kind, ResourceKind.TERMBASE)
            self.assertEqual(terms.path.suffix, ".csv")
            with self.assertRaisesRegex(ResourceError, "unsupported resource kind"):
                repository.create_resource("Unknown", "not-a-resource")

    def test_updates_state_atomically_and_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            repository = ResourceRepository(config_dir)
            resource = repository.create_resource("Read only", ResourceKind.TRANSLATION_MEMORY)
            changed = replace(resource, active=False, lookup=False, update=True)

            repository.update_resource(changed)
            restored = ResourceRepository(config_dir).get(resource.id)
            payload = json.loads((config_dir / "resources.json").read_text(encoding="utf-8"))
            resource_path_exists = restored.path.exists()

        self.assertFalse(restored.active)
        self.assertFalse(restored.lookup)
        self.assertTrue(restored.update)
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(resource_path_exists)

    def test_rejects_unknown_or_tampered_resource_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource("Main", ResourceKind.TRANSLATION_MEMORY)

            with self.assertRaises(ResourceError):
                repository.update_resource(replace(resource, id="missing"))
            with self.assertRaises(ResourceError):
                repository.update_resource(replace(resource, path=(root / "elsewhere.jsonl").resolve()))
            with self.assertRaises(ResourceError):
                repository.create_resource("  ", ResourceKind.TERMBASE)

    def test_deletes_managed_resource_and_persists_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            repository = ResourceRepository(config_dir)
            resource = repository.create_resource("Disposable", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text('{"source":"A","target":"甲"}\n', encoding="utf-8")

            deleted = repository.delete_resource(resource.id)
            restored = ResourceRepository(config_dir)

            self.assertEqual(deleted, resource)
            self.assertFalse(resource.path.exists())
            self.assertEqual(restored.list_resources(), ())

    def test_deleting_external_resource_only_unregisters_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tm_path, _ = self._make_defaults(root)
            repository = ResourceRepository(root / "app-data", default_tm_path=tm_path)

            repository.delete_resource("local-tm")

            self.assertTrue(tm_path.exists())
            self.assertEqual(repository.list_resources(), ())

    def test_tampered_dotdot_path_is_never_treated_as_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            external = config_dir / "outside.jsonl"
            external.parent.mkdir(parents=True)
            external.write_text("keep\n", encoding="utf-8")
            registry = config_dir / "resources.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "resources": [
                            {
                                "id": "tampered",
                                "name": "Tampered",
                                "kind": "translation_memory",
                                "path": str(config_dir / "resources" / ".." / "outside.jsonl"),
                                "active": True,
                                "lookup": True,
                                "update": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            repository = ResourceRepository(config_dir)

            repository.delete_resource("tampered")

            self.assertTrue(external.exists())

    def test_delete_rolls_back_managed_file_if_registry_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ResourceRepository(Path(temp_dir) / "app-data")
            resource = repository.create_resource("Keep", ResourceKind.TRANSLATION_MEMORY)
            original = resource.path.read_bytes()

            with patch.object(
                repository,
                "_write_registry",
                side_effect=ResourceError("registry unavailable"),
            ):
                with self.assertRaisesRegex(ResourceError, "registry unavailable"):
                    repository.delete_resource(resource.id)

            self.assertEqual(resource.path.read_bytes(), original)
            self.assertEqual(repository.get(resource.id), resource)

    def test_corrupt_registry_is_reported_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            registry = config_dir / "resources.json"
            registry.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(ResourceError):
                ResourceRepository(config_dir)

            self.assertEqual(registry.read_text(encoding="utf-8"), "{not-json")


if __name__ == "__main__":
    unittest.main()
