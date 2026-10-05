"""直接验证 Chunk 的 owner 依赖边界，不消费源码摘要快照。"""

from __future__ import annotations

from pathlib import Path
import unittest

from tests.parser_architecture_test_support import collect_import_references


ROOT = Path(__file__).resolve().parents[1]
OWNED = (
    "chunk_controller_contracts.py",
    "collaborative_chunk_contracts.py",
    "collaborative_chunk_store.py",
    "collaborative_chunk_workspace_adapter.py",
    "collaborative_chunks.py",
    "collaborative_chunk_conflict.py",
    "chunk_controller_adapter.py",
    "qt_chunk_manager_dialog.py",
)
FORBIDDEN = (
    "parser_",
    "project_package",
    "resource_",
    "tm_engine",
    "tm_store",
    "tm_candidate",
    "tm_retrieval",
    "tm_fuzzy",
    "tmx_",
)


def _boundary_violations(relative: str, source: str) -> tuple[str, ...]:
    forbidden = FORBIDDEN
    imports = collect_import_references(source, module_name=Path(relative).stem)
    return tuple(
        reference.target
        for reference in imports
        if reference.target.startswith(forbidden)
        or "provider" in reference.target.lower()
        or "sync" in reference.target.lower()
    )


class CollaborativeChunksArchitectureTests(unittest.TestCase):
    def test_current_owner_sources_keep_their_dependency_boundary(self) -> None:
        for relative in OWNED:
            with self.subTest(source=relative):
                source = (ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(_boundary_violations(relative, source), ())

    def test_boundary_detects_static_aliased_and_literal_dynamic_imports(self) -> None:
        for relative in OWNED:
            forbidden = FORBIDDEN
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

    def test_sync_authority_is_rejected_regardless_of_module_position(self) -> None:
        for source in (
            "import cross_device_sync",
            "from sync import publish",
            "from integrations.sync import publish",
            "import importlib\nimportlib.import_module('cross_device_sync')",
            "from importlib import import_module as load\nload('integrations.sync')",
        ):
            with self.subTest(source=source):
                self.assertTrue(
                    _boundary_violations("collaborative_chunks.py", source)
                )


if __name__ == "__main__":
    unittest.main()
