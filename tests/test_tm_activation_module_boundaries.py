from __future__ import annotations

import ast
import py_compile
import sys
import unittest
from pathlib import Path

import tm_activation_journal
import tm_activation_recovery
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


class TMActivationModuleBoundariesTest(unittest.TestCase):
    def test_recovery_module_never_imports_the_store(self) -> None:
        path = PROJECT_ROOT / "tm_activation_recovery.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        self.assertNotIn("tm_sqlite_store", modules)
        self.assertFalse(
            any(module.startswith("tm_sqlite_store") for module in modules)
        )
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("SQLiteTMStore", source)
        self.assertNotIn("ResourceStoreCoordinator", source)

    def test_journal_module_never_imports_the_store(self) -> None:
        path = PROJECT_ROOT / "tm_activation_journal.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        self.assertNotIn("tm_sqlite_store", modules)
        self.assertFalse(
            any(module.startswith("tm_sqlite_store") for module in modules)
        )

    def test_recovery_module_imports_only_contracts_journal_and_stdlib(self) -> None:
        modules = _imported_modules(PROJECT_ROOT / "tm_activation_recovery.py")
        self.assertIn("tm_contracts", modules)
        self.assertIn("tm_activation_journal", modules)
        for module in modules:
            if module in {"tm_contracts", "tm_activation_journal"}:
                continue
            if module.startswith("tm_contracts"):
                continue
            self.assertIn(module, sys.stdlib_module_names, module)

    def test_store_reexports_former_private_activation_names(self) -> None:
        for name in (
            "_SQLiteGenerationView",
            "_ActivationPreparation",
            "_ActivationJournalHandle",
            "_ActivationJournalRecord",
            "_ActivationJournalPhase",
            "_activation_journal_path",
            "_read_activation_journal_file",
            "_write_activation_journal_bytes",
            "_publish_activation_receipt",
            "_publish_activation_manifest",
            "_replace_activation_database",
            "_validate_replaced_activation_database",
            "_validate_published_activation_set",
            "publish_activation",
            "recover_durable_activation",
            "rollback_durable_activation",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(tm_sqlite_store, name), name)
        self.assertIs(
            tm_sqlite_store._ActivationJournalRecord,
            tm_activation_journal._ActivationJournalRecord,
        )
        self.assertIs(
            tm_sqlite_store._publish_activation_receipt,
            tm_activation_recovery._publish_activation_receipt,
        )


if __name__ == "__main__":
    unittest.main()
