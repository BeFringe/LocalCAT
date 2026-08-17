"""Feature 5 dependency, privacy, and capability-ownership guards."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from tests.acceptance_matrix_registry import (
    CONSUMER_GUARD_PATHS as CONSUMER_PATHS,
    FEATURE5_CORE_GUARD_PATHS as FEATURE5_CORE_PATHS,
)


_ROOT = Path(__file__).resolve().parent.parent

_NETWORK_IMPORTS = {
    "aiohttp",
    "boto3",
    "ftplib",
    "google",
    "http",
    "httpx",
    "imaplib",
    "paramiko",
    "poplib",
    "requests",
    "sentry_sdk",
    "smtplib",
    "socket",
    "telnetlib",
    "urllib",
}
_IDENTITY_SERVICE_NAMES = {
    "account",
    "api_key",
    "credential",
    "credentials",
    "oauth",
    "telemetry",
}
_FORBIDDEN_CORE_IMPORT_PREFIXES = (
    "PySide6",
    "glossary_engine",
    "parser",
    "qt_",
    "resource_importer",
    "tmx",
)
_FORBIDDEN_CONSUMER_IMPORT_PREFIXES = (
    "matcher_capability",
    "text_matcher",
    "tm_benchmark",
    "tm_candidate_index",
    "tm_retrieval_capability",
    "tm_retrieval_validation",
)
_FORBIDDEN_CONSUMER_NAMES = {
    "MatcherCapabilityEvaluator",
    "MatcherCapabilityPublisher",
    "RetrievalCapabilityEvaluator",
    "RetrievalCapabilityPublisher",
    "TextMatcherV1",
    "recompute_matcher_validation",
    "recompute_retrieval_validation",
    "validation_summary",
}
_FORBIDDEN_CONSUMER_LITERALS = {
    "matcher_readiness",
    "retrieval_readiness",
    "validation_summary",
}


def _tree(relative: str) -> ast.Module:
    path = _ROOT / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=relative)


def _imports(tree: ast.AST) -> tuple[str, ...]:
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            imported.append(node.args[0].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
        ):
            imported.append(node.args[0].value)
    return tuple(imported)


def _identifiers(tree: ast.AST) -> set[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            values.add(node.id)
        elif isinstance(node, ast.Attribute):
            values.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            values.add(node.name)
    return values


def _string_literals(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and type(node.value) is str
    }


class TMArchitectureGuardTests(unittest.TestCase):
    def test_feature5_core_file_set_is_closed_and_regular(self) -> None:
        self.assertEqual(
            len(FEATURE5_CORE_PATHS),
            len(set(FEATURE5_CORE_PATHS)),
        )
        for relative in FEATURE5_CORE_PATHS + CONSUMER_PATHS:
            path = _ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(path.resolve(strict=True).parent, _ROOT)

    def test_core_has_no_network_account_telemetry_or_credentials(self) -> None:
        for relative in FEATURE5_CORE_PATHS:
            tree = _tree(relative)
            roots = {name.partition(".")[0] for name in _imports(tree)}
            self.assertEqual(roots & _NETWORK_IMPORTS, set(), relative)
            self.assertEqual(
                _identifiers(tree) & _IDENTITY_SERVICE_NAMES,
                set(),
                relative,
            )

    def test_core_does_not_import_qt_parser_tmx_or_glossary(self) -> None:
        for relative in FEATURE5_CORE_PATHS:
            for imported in _imports(_tree(relative)):
                self.assertFalse(
                    imported.startswith(_FORBIDDEN_CORE_IMPORT_PREFIXES),
                    f"{relative}: {imported}",
                )

    def test_consumers_do_not_own_readiness_or_bypass_gated_matcher(
        self,
    ) -> None:
        for relative in CONSUMER_PATHS:
            tree = _tree(relative)
            for imported in _imports(tree):
                self.assertFalse(
                    imported.startswith(
                        _FORBIDDEN_CONSUMER_IMPORT_PREFIXES
                    ),
                    f"{relative}: {imported}",
                )
            self.assertEqual(
                _identifiers(tree) & _FORBIDDEN_CONSUMER_NAMES,
                set(),
                relative,
            )
            self.assertEqual(
                _string_literals(tree) & _FORBIDDEN_CONSUMER_LITERALS,
                set(),
                relative,
            )


if __name__ == "__main__":
    unittest.main()
