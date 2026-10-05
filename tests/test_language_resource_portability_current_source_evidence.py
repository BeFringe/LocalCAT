"""直接验证 ResourcePackage 的 owner 依赖边界，不消费源码摘要快照。"""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.parser_architecture_test_support import collect_import_references


ROOT = Path(__file__).resolve().parents[1]
OWNED = (
    "resource_package_contracts.py",
    "resource_package.py",
    "resource_artifact_save.py",
    "resource_platform_io.py",
    "resource_receipt_ledger.py",
    "resource_portability.py",
    "tm_resource_port.py",
)
FORBIDDEN = {
    "resource_package_contracts.py": (
        "editor_", "project_package", "qt_", "resource_repository",
        "termbase_store", "tm_",
    ),
    "resource_package.py": (
        "editor_", "project_package", "qt_", "resource_repository",
        "termbase_store", "tm_",
    ),
    "resource_artifact_save.py": (
        "editor_", "project_package", "qt_", "resource_repository",
        "termbase_store", "tm_",
    ),
    "resource_platform_io.py": (
        "editor_", "platform_fs_posix", "platform_fs_windows", "project_",
        "qt_", "resource_", "sync_", "termbase_", "tm_", "tmx_",
    ),
    "resource_receipt_ledger.py": (
        "editor_", "project_package", "qt_", "resource_repository",
        "termbase_store", "tm_",
    ),
    "resource_portability.py": ("project_package", "qt_", "sync_", "tmx_"),
    "tm_resource_port.py": ("project_package", "qt_", "sync_", "tmx_"),
}


def _boundary_violations(relative: str, source: str) -> tuple[str, ...]:
    forbidden = FORBIDDEN[relative]
    imports = collect_import_references(source, module_name=Path(relative).stem)
    return tuple(
        reference.target
        for reference in imports
        if reference.target.startswith(forbidden)
        or "provider" in reference.target.lower()
        or reference.target.startswith("sync_")
    )


class LanguageResourcePortabilityArchitectureTests(unittest.TestCase):
    def test_current_owner_sources_keep_their_dependency_boundary(self) -> None:
        for relative in OWNED:
            with self.subTest(source=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(_boundary_violations(relative, source), ())

    def test_boundary_detects_static_aliased_and_literal_dynamic_imports(self) -> None:
        for relative in OWNED:
            forbidden = FORBIDDEN[relative]
            for prefix in (*forbidden, "provider_", "sync_"):
                module = prefix + "foreign" if prefix.endswith("_") else prefix
                for source in (
                    f"import {module} as dependency\n",
                    f"from {module} import foreign as dependency\n",
                    f"from importlib import import_module as load\nload('{module}')\n",
                ):
                    with self.subTest(owner=relative, source=source):
                        self.assertTrue(_boundary_violations(relative, source))

    def test_comments_strings_and_safe_imports_do_not_change_the_boundary(self) -> None:
        source = '# import project_package\nlabel = "import provider_foreign"\nimport dataclasses\n'
        for relative in OWNED:
            with self.subTest(source=relative):
                self.assertEqual(_boundary_violations(relative, source), ())


if __name__ == "__main__":
    unittest.main()
