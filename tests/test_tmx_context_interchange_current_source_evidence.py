"""直接验证 TMX 的 owner 依赖边界，不消费源码摘要快照。"""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.parser_architecture_test_support import collect_import_references


ROOT = Path(__file__).resolve().parents[1]
OWNED = (
    "tmx_context_contracts.py",
    "tmx_context_interchange.py",
    "tmx_artifact_save.py",
    "tmx_bound_artifact_save.py",
    "tmx_platform_io.py",
    "tmx_export_scope_contracts.py",
    "tmx_export_coordinator.py",
)
FORBIDDEN = {
    "tmx_context_contracts.py": (
        "collaborative_", "editor_", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_context_interchange.py": (
        "collaborative_", "editor_", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_artifact_save.py": (
        "collaborative_", "editor_", "parser_", "project_", "qt_",
        "resource_", "tm_",
    ),
    "tmx_bound_artifact_save.py": (
        "collaborative_", "editor_", "parser_", "platform_fs_posix",
        "platform_fs_windows", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_platform_io.py": (
        "collaborative_", "editor_", "parser_", "platform_fs_posix",
        "platform_fs_windows", "project_", "qt_", "resource_", "tm_",
    ),
    "tmx_export_scope_contracts.py": ("editor_", "qt_", "resource_"),
    "tmx_export_coordinator.py": ("editor_", "qt_", "resource_"),
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


class TmxArchitectureTests(unittest.TestCase):
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
