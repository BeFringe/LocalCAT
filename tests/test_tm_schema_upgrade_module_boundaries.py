"""Task 5.R2 schema-upgrade module boundary tests.

The suite proves the Task 5.R2 layout-only extraction held its
dependency direction: ``tm_schema_upgrade.py`` never imports
``tm_sqlite_store`` or ``tm_migration`` (statically or through literal
dynamic imports), the owner entry points remain in their modules, and
the old private names stay behind as late-bound compatibility wrappers
so existing patch/fault-injection seams keep observing the owner
namespaces.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import py_compile
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import tm_migration
import tm_schema_upgrade
import tm_sqlite_store


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _dynamic_imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            modules.add(first.value)
    return modules


_STORE_OWNED_SCHEMA_UPGRADE_NAMES = (
    "_SchemaUpgradeSnapshotTicket",
    "_SchemaUpgradeLocatorSnapshot",
    "ResourceStoreCoordinator",
    "_require_schema_upgrade_ancestry_provable",
    "_require_no_pending_activation_assets",
    "_require_schema_upgrade_mode_closure",
    "_require_schema_upgrade_ticket_guard",
    "_recovered_schema_upgrade_pending_root",
    "_legacy_completed_origin_blocks",
    "_legacy_revision_ancestry",
)

_MIGRATION_OWNED_SCHEMA_UPGRADE_NAMES = (
    "MigrationPreflightError",
    "TMMigrationService",
)

# Every private name that moved to tm_schema_upgrade must remain in the
# owner namespaces as a late-bound compatibility wrapper.
_MOVED_STORE_NAMES = (
    "_schema_upgrade_backup_path",
    "_fsync_schema_upgrade_directory",
    "_create_schema_upgrade_backup",
    "_remove_partial_schema_upgrade_backup",
    "_schema_upgrade_locator_snapshot_path",
    "_create_schema_upgrade_locator_snapshot",
    "_remove_owned_schema_upgrade_artifact",
    "_schema_upgrade_reported_path",
    "_promote_schema_upgrade_artifact",
    "_pending_schema_upgrade_family",
    "_require_owned_pending_schema_upgrade_name",
    "_sweep_pending_schema_upgrade_artifacts",
    "_promote_pending_schema_upgrade_backup",
    "_finish_cold_schema_upgrade_pending",
    "_remove_schema_upgrade_backup",
    "_remove_schema_upgrade_locator_snapshot",
    "_file_sha256_of_path",
)

_MOVED_MIGRATION_NAMES = (
    "_strict_locator_proof",
    "_schema_version_of_store",
    "_upgrade_source_ref",
    "_open_legacy_read_connection",
    "_read_active_activation_digest",
    "_read_legacy_snapshot_facts",
    "_copy_store_into_stage",
    "_migrate_schema_copy",
    "_remove_owned_schema_upgrade_backup",
)


class TMSchemaUpgradeModuleBoundariesTest(unittest.TestCase):
    def test_schema_upgrade_module_never_imports_the_owners(self) -> None:
        path = PROJECT_ROOT / "tm_schema_upgrade.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        self.assertNotIn("tm_sqlite_store", modules)
        self.assertNotIn("tm_migration", modules)
        self.assertFalse(
            any(module.startswith("tm_sqlite_store") for module in modules)
        )
        self.assertFalse(
            any(module.startswith("tm_migration") for module in modules)
        )
        self.assertEqual(_dynamic_imported_modules(path), set())
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("SQLiteTMStore", source)

    def test_schema_upgrade_module_imports_only_allowed_family(self) -> None:
        path = PROJECT_ROOT / "tm_schema_upgrade.py"
        modules = _imported_modules(path)
        self.assertIn("tm_contracts", modules)
        self.assertIn("tm_activation_journal", modules)
        for module in modules:
            if module in {"tm_contracts", "tm_activation_journal"}:
                continue
            if module.startswith("tm_contracts"):
                continue
            if module in {"collections.abc"} or module.startswith("collections"):
                continue
            self.assertIn(module, sys.stdlib_module_names, module)

    def test_store_keeps_schema_upgrade_authority_entry_points(self) -> None:
        coordinator = tm_sqlite_store.ResourceStoreCoordinator
        for name in _STORE_OWNED_SCHEMA_UPGRADE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_sqlite_store, name), name)
        for method in (
            "prepare_schema_upgrade_ticket",
            "retire_schema_upgrade_ticket",
            "schema_upgrade_locator_snapshot",
            "release_schema_upgrade_locator_snapshot",
        ):
            with self.subTest(method=method):
                self.assertTrue(hasattr(coordinator, method), method)

    def test_migration_keeps_schema_upgrade_orchestration_entry_points(
        self,
    ) -> None:
        for name in _MIGRATION_OWNED_SCHEMA_UPGRADE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_migration, name), name)
        self.assertTrue(
            hasattr(tm_migration.TMMigrationService, "upgrade_schema")
        )

    def test_moved_store_names_stay_late_bound_wrappers(self) -> None:
        for name in _MOVED_STORE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_sqlite_store, name), name)

    def test_moved_migration_names_stay_late_bound_wrappers(self) -> None:
        for name in _MOVED_MIGRATION_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_migration, name), name)
        self.assertTrue(hasattr(tm_migration, "_promote_schema_upgrade_artifact"))

    def test_store_wrapper_is_late_bound_to_the_new_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "tm_schema_upgrade._schema_upgrade_reported_path",
                return_value=root / "probed.stable",
            ) as probed:
                result = tm_sqlite_store._schema_upgrade_reported_path(
                    root / "probed.stable.pending"
                )
            probed.assert_called_once_with(root / "probed.stable.pending")
            self.assertEqual(result, root / "probed.stable")

    def test_migration_wrapper_is_late_bound_to_the_new_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"owned bytes"
            candidate = root / "candidate.bin"
            candidate.write_bytes(payload)
            with patch(
                "tm_schema_upgrade._strict_locator_proof",
                return_value=((7, 9), "probe"),
            ) as probed:
                result = tm_migration._strict_locator_proof(
                    candidate,
                    "ignored",
                )
            probed.assert_called_once_with(candidate, "ignored")
            self.assertEqual(result, ((7, 9), "probe"))

    def test_copy_plan_snapshots_current_owner_callbacks(self) -> None:
        def completed_blocks_probe(
            _connection: object,
        ) -> tuple[tuple[str, int, int], ...]:
            return ()

        with patch(
            "tm_migration._legacy_completed_origin_blocks",
            new=completed_blocks_probe,
        ):
            plan = tm_migration._schema_upgrade_copy_plan()
        self.assertIs(
            plan.completed_origin_blocks,
            completed_blocks_probe,
        )
        self.assertEqual(
            dict(plan.approved_schema_digests),
            dict(tm_migration._APPROVED_SCHEMA_DIGESTS),
        )
        with self.assertRaises(TypeError):
            cast(Any, plan.approved_schema_digests)[False] = "mutated"

    def test_strict_locator_proof_equivalence_across_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = os.urandom(64)
            candidate = root / "owned.bin"
            candidate.write_bytes(payload)
            import hashlib

            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(
                tm_sqlite_store._file_sha256_of_path(candidate),
                tm_schema_upgrade._file_sha256_of_path(candidate),
            )
            self.assertEqual(
                tm_migration._strict_locator_proof(candidate, digest),
                tm_schema_upgrade._strict_locator_proof(candidate, digest),
            )
            first_root = root / "one"
            second_root = root / "two"
            first_root.mkdir()
            second_root.mkdir()
            first = first_root / "x.pending"
            second = second_root / "x.pending"
            first.write_bytes(b"promote me")
            second.write_bytes(b"promote me")
            first_identity = (os.lstat(first).st_dev, os.lstat(first).st_ino)
            second_identity = (os.lstat(second).st_dev, os.lstat(second).st_ino)
            self.assertEqual(
                tm_sqlite_store._promote_schema_upgrade_artifact(
                    first,
                    first_identity,
                ).name,
                tm_schema_upgrade._promote_schema_upgrade_artifact(
                    second,
                    second_identity,
                ).name,
            )


if __name__ == "__main__":
    unittest.main()
