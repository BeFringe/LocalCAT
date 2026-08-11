"""Task 7.4 retrieval-capability dependency boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path
import py_compile
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CAPABILITY = "tm_retrieval_capability"
_FORBIDDEN_CAPABILITY_IMPORTS = (
    "tm_candidate_index",
    "tm_migration",
    "tm_retrieval",
    "tm_sqlite_store",
)
_PHYSICAL_OR_RECALL_OWNERS = (
    "tm_candidate_index.py",
    "tm_migration.py",
    "tm_sqlite_store.py",
)


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
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            modules.add(first.value)
    return modules


class TMRetrievalCapabilityModuleBoundariesTest(unittest.TestCase):
    def test_capability_module_depends_only_on_contracts_and_stdlib(
        self,
    ) -> None:
        path = PROJECT_ROOT / f"{_CAPABILITY}.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        self.assertIn("tm_contracts", modules)
        for forbidden in _FORBIDDEN_CAPABILITY_IMPORTS:
            self.assertNotIn(forbidden, modules)
            self.assertFalse(
                any(module.startswith(forbidden) for module in modules)
            )
        self.assertEqual(_dynamic_imported_modules(path), set())
        for module in modules:
            if module == "tm_contracts" or module.startswith("tm_contracts"):
                continue
            self.assertIn(module, sys.stdlib_module_names, module)

    def test_physical_and_recall_owners_do_not_import_capability(self) -> None:
        for filename in _PHYSICAL_OR_RECALL_OWNERS:
            with self.subTest(filename=filename):
                path = PROJECT_ROOT / filename
                modules = _imported_modules(path)
                self.assertNotIn(_CAPABILITY, modules)
                self.assertFalse(
                    any(
                        module.startswith(_CAPABILITY)
                        for module in modules
                    )
                )
                self.assertNotIn(
                    _CAPABILITY,
                    _dynamic_imported_modules(path),
                )


if __name__ == "__main__":
    unittest.main()
