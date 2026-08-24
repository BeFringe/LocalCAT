from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


class ResourcePortabilityArchitectureTests(unittest.TestCase):
    def test_leaf_contracts_and_carrier_do_not_import_project_or_resource_owners(self) -> None:
        contract_imports = _imports(ROOT / "resource_package_contracts.py")
        carrier_imports = _imports(ROOT / "resource_package.py")
        forbidden = {
            "project_package",
            "editor_controller",
            "editor_contracts",
            "termbase_store",
            "tm_migration",
            "tm_sqlite_store",
            "resource_repository",
        }
        self.assertFalse(contract_imports & forbidden)
        self.assertFalse(carrier_imports & forbidden)

    def test_project_package_and_resource_package_do_not_depend_on_each_other(self) -> None:
        self.assertNotIn("project_package", _imports(ROOT / "resource_package.py"))
        self.assertNotIn("resource_package", _imports(ROOT / "project_package.py"))

    def test_qt_never_imports_zip_or_owner_storage_modules(self) -> None:
        imports = _imports(ROOT / "qt_settings_dialog.py")
        self.assertFalse(
            imports
            & {
                "zipfile",
                "resource_package",
                "termbase_store",
                "tm_migration",
                "tm_sqlite_store",
            }
        )

    def test_package_owner_does_not_implement_tmx_or_import_its_grammar(self) -> None:
        carrier_imports = _imports(ROOT / "resource_package.py")
        orchestration_imports = _imports(ROOT / "resource_portability.py")
        self.assertFalse(
            carrier_imports
            & {
                "parser_tmx_codec",
                "resource_importer",
                "tmx_context_interchange",
                "xml",
                "xml.etree.ElementTree",
            }
        )
        self.assertFalse(
            orchestration_imports
            & {
                "parser_tmx_codec",
                "resource_importer",
                "tmx_context_interchange",
                "xml",
                "xml.etree.ElementTree",
            }
        )
        port_source = (ROOT / "resource_payload_port.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("class ResourcePackagePayloadHandler", port_source)
        self.assertNotIn("ElementTree", port_source)
        self.assertFalse(
            _imports(ROOT / "resource_payload_port.py")
            & {
                "resource_package",
                "resource_portability",
                "parser_tmx_codec",
                "resource_importer",
                "tmx_context_interchange",
            }
        )


if __name__ == "__main__":
    unittest.main()
