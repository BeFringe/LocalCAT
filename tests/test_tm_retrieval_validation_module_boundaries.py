"""Task 7.5 tm_retrieval_validation dependency direction tests.

The offline Gate C validation leaf may import ``tm_retrieval``,
``tm_retrieval_capability``, ``tm_contracts``, ``tm_gate_a``,
``tm_candidate_index`` and ``tm_sqlite_store`` only; it must never import
tests, migration, facade or Qt modules, and no production runtime module
may import the validation leaf.
"""

from __future__ import annotations

import ast
from pathlib import Path
import py_compile
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VALIDATION = "tm_retrieval_validation"
_ALLOWED_VALIDATION_DEPENDENCIES = (
    "tm_candidate_index",
    "tm_contracts",
    "tm_gate_a",
    "tm_retrieval",
    "tm_retrieval_capability",
    "tm_sqlite_store",
)
_FORBIDDEN_VALIDATION_IMPORTS = (
    "PySide6",
    "tests",
    "tm_engine",
    "tm_migration",
    "xlwings",
)
_PROJECT_NAMESPACES = (
    "tm_activation",
    "tm_candidate",
    "tm_engine",
    "tm_gate",
    "tm_json",
    "tm_legacy",
    "tm_migration",
    "tm_retrieval",
    "tm_schema",
    "tm_similarity",
    "tm_snapshot",
    "tm_sqlite",
    "tm_stage",
    "tools",
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


class TMRetrievalValidationModuleBoundariesTest(unittest.TestCase):
    def test_validation_leaf_depends_only_on_approved_modules(self) -> None:
        path = PROJECT_ROOT / f"{_VALIDATION}.py"
        py_compile.compile(str(path), doraise=True)
        modules = _imported_modules(path)
        for approved in _ALLOWED_VALIDATION_DEPENDENCIES:
            self.assertIn(approved, modules)
        for forbidden in _FORBIDDEN_VALIDATION_IMPORTS:
            self.assertNotIn(forbidden, modules)
            self.assertFalse(
                any(module.startswith(forbidden) for module in modules)
            )
        self.assertEqual(_dynamic_imported_modules(path), set())
        for module in modules:
            if module in _ALLOWED_VALIDATION_DEPENDENCIES:
                continue
            self.assertIn(
                module.split(".")[0],
                sys.stdlib_module_names,
                module,
            )

    def test_no_production_runtime_module_imports_validation(self) -> None:
        for path in sorted(PROJECT_ROOT.glob("*.py")):
            if path.name == f"{_VALIDATION}.py":
                continue
            with self.subTest(filename=path.name):
                modules = _imported_modules(path)
                self.assertNotIn(_VALIDATION, modules)
                self.assertFalse(
                    any(
                        module.startswith(_VALIDATION)
                        for module in modules
                    )
                )
                self.assertNotIn(
                    _VALIDATION,
                    _dynamic_imported_modules(path),
                )

    def test_validation_leaf_imports_no_runtime_side_effect_modules(
        self,
    ) -> None:
        path = PROJECT_ROOT / f"{_VALIDATION}.py"
        modules = _imported_modules(path)
        for namespace in _PROJECT_NAMESPACES:
            if namespace in _ALLOWED_VALIDATION_DEPENDENCIES:
                continue
            self.assertFalse(
                any(
                    module == namespace or module.startswith(namespace + ".")
                    for module in modules
                ),
                namespace,
            )


if __name__ == "__main__":
    unittest.main()
