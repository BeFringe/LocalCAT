"""Wave 0 tests for the reusable Parser AST/import architecture harness.

Only synthetic modules are checked here.  Production-tree enforcement belongs
to later waves after the Parser modules exist and legacy grammars are migrated.
"""

from __future__ import annotations

import unittest

from tests.parser_architecture_test_support import (
    AllowedImportRule,
    ArchitectureConfigurationError,
    ArchitecturePolicy,
    DEFERRED_BOUNDARY_MATRIX,
    DeferredBoundary,
    ExclusiveCallRule,
    ForbiddenImportRule,
    ImportMechanism,
    PARSER_MODULE_PREFIXES,
    SourceModule,
    build_parser_architecture_policy,
    collect_import_references,
    module_matches_prefix,
)


class ImportExtractionTests(unittest.TestCase):
    def test_static_and_literal_importlib_imports_are_structural(self) -> None:
        references = collect_import_references(
            """
import tm_engine
from tm_sqlite_store import TMStore as Store
import importlib as loader
from importlib import import_module as load

loader.import_module("workspace.state")
load("sync_provider")
""",
            module_name="parser_tmx_codec",
        )

        observed = {
            (reference.target, reference.symbol, reference.mechanism)
            for reference in references
        }
        self.assertIn(("tm_engine", None, ImportMechanism.STATIC), observed)
        self.assertIn(
            ("tm_sqlite_store.TMStore", "TMStore", ImportMechanism.STATIC),
            observed,
        )
        self.assertIn(
            ("workspace.state", None, ImportMechanism.LITERAL_IMPORTLIB),
            observed,
        )
        self.assertIn(
            ("sync_provider", None, ImportMechanism.LITERAL_IMPORTLIB),
            observed,
        )

    def test_comments_strings_lookalikes_and_nonliteral_imports_are_not_grep_hits(self) -> None:
        references = collect_import_references(
            '''
# import tm_store
EXAMPLE = "import workspace; importlib.import_module('sync_provider')"

class Helper:
    def import_module(self, name):
        return name

helper = Helper()
helper.import_module("tm_store")
import importlib
name = "tm_store"
importlib.import_module(name)
''',
            module_name="parser_contracts",
        )

        self.assertEqual(
            {reference.target for reference in references},
            {"importlib"},
        )

    def test_relative_imports_are_resolved_for_package_shaped_synthetic_modules(self) -> None:
        references = collect_import_references(
            "from . import parser_contracts\nfrom ..shared import contracts\n",
            module_name="localcat.parser.codec",
        )
        self.assertEqual(
            {reference.target for reference in references},
            {"localcat.parser.parser_contracts", "localcat.shared.contracts"},
        )


class ParserDependencyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_parser_architecture_policy()

    def test_approved_dependency_direction_has_no_synthetic_violation(self) -> None:
        modules = (
            SourceModule("parser_contracts", "import dataclasses\nimport typing\n"),
            SourceModule("parser_source", "from parser_contracts import FormatId\n"),
            SourceModule(
                "parser_localcat_codec",
                "from parser_contracts import ParsedDocument\nimport parser_source\n",
            ),
            SourceModule(
                "parser_composition",
                "import parser_registry\n"
                "import parser_source\n"
                "import parser_localcat_codec\n",
            ),
            SourceModule(
                "editor_project",
                "import parser_contracts\n"
                "import parser_composition\n"
                "import editor_controller\n",
            ),
            SourceModule(
                "rpy_project_codec",
                "from parser_contracts import ParsedDocument\n",
            ),
        )

        self.assertEqual(self.policy.check_modules(modules), ())

    def test_reverse_and_internal_layer_imports_are_reported(self) -> None:
        modules = (
            SourceModule("parser_contracts", "import parser_source\n"),
            SourceModule("parser_registry", "import parser_tmx_codec\n"),
            SourceModule(
                "parser_tmx_codec",
                "import tm_engine\nimport tm_sqlite_store\n",
            ),
            SourceModule("tm_sqlite_store", "import parser_contracts\n"),
        )

        observed = {violation.rule_id for violation in self.policy.check_modules(modules)}

        self.assertEqual(
            observed,
            {
                "contracts.allowed_dependencies",
                "registry.allowed_dependencies",
                "tmx_codec.allowed_dependencies",
                "engine_store.must_not_import_parser",
                "deferred.tm_storage_authority",
            },
        )

    def test_literal_dynamic_import_cannot_bypass_dependency_rule(self) -> None:
        violations = self.policy.check_module(
            SourceModule(
                "parser_source",
                "import importlib as il\nil.import_module('parser_registry')\n",
            )
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "source.allowed_dependencies")
        self.assertEqual(violations[0].mechanism, "literal_importlib")

    def test_importlib_dotted_binding_and_one_step_alias_keep_literal_guarded(self) -> None:
        dotted = self.policy.check_module(
            SourceModule(
                "parser_source",
                "import importlib.util\nimportlib.import_module('parser_registry')\n",
            )
        )
        one_step_alias = self.policy.check_module(
            SourceModule(
                "parser_source",
                "from importlib import import_module\n"
                "load = import_module\n"
                "load('parser_registry')\n",
            )
        )
        nonliteral = self.policy.check_module(
            SourceModule(
                "parser_source",
                "import importlib.util\n"
                "name = 'parser_registry'\n"
                "importlib.import_module(name)\n",
            )
        )

        for findings in (dotted, one_step_alias):
            self.assertIn(
                ("source.allowed_dependencies", "literal_importlib"),
                {(item.rule_id, item.mechanism) for item in findings},
            )
        self.assertEqual(nonliteral, ())

    def test_only_composition_may_import_builtin_codecs_through_registry_rule(self) -> None:
        composition = self.policy.check_module(
            SourceModule("parser_composition", "import parser_tmx_codec\n")
        )
        registry = self.policy.check_module(
            SourceModule("parser_registry", "import parser_tmx_codec\n")
        )

        self.assertEqual(composition, ())
        self.assertEqual(
            [violation.rule_id for violation in registry],
            ["registry.allowed_dependencies"],
        )

    def test_neutral_contracts_reject_every_non_stdlib_dependency(self) -> None:
        for target in ("requests", "openpyxl", "project_local_module"):
            with self.subTest(target=target):
                violations = self.policy.check_module(
                    SourceModule("parser_contracts", f"import {target}\n")
                )
                self.assertEqual(
                    [violation.rule_id for violation in violations],
                    ["contracts.allowed_dependencies"],
                )

        self.assertEqual(
            self.policy.check_module(
                SourceModule(
                    "parser_contracts",
                    "from __future__ import annotations\nimport dataclasses\nimport typing\n",
                )
            ),
            (),
        )

    def test_application_and_plugin_cannot_bypass_the_neutral_port(self) -> None:
        findings = self.policy.check_modules(
            (
                SourceModule("editor_project", "import parser_localcat_codec\n"),
                SourceModule("rpy_project_codec", "import parser_source\n"),
                SourceModule("rpy_tokens", "import tm_engine\n"),
            )
        )

        self.assertEqual(
            {violation.rule_id for violation in findings},
            {
                "application.parser_surface_only",
                "plugin.depends_on_neutral_contract_only",
                "plugin.must_not_import_localcat_authorities",
            },
        )

    def test_application_cannot_construct_or_reach_behind_the_parser_surface(self) -> None:
        findings = self.policy.check_module(
            SourceModule(
                "editor_project",
                "import parser_composition as parser\n"
                "parser.ParserApplicationSurface(object())\n"
                "parser.OpenedParserInput(object())\n"
                "parser.PreparedCanonicalWrite(b'raw')\n"
                "parser.ParserRegistry(())\n"
                "parser.CanonicalBytes()\n"
                "parser.GuardedParseSession()\n"
                "parser.atomic_write_bytes()\n",
            )
        )

        self.assertEqual(
            [finding.rule_id for finding in findings],
            ["application.parser_surface_factory_only"] * 7,
        )

    def test_each_parser_layer_rejects_dependencies_outside_its_allowlist(self) -> None:
        cases = (
            ("parser_source", "import openpyxl\n", "source.allowed_dependencies"),
            ("parser_registry", "import requests\n", "registry.allowed_dependencies"),
            (
                "parser_json_support",
                "import editor_project\n",
                "json_support.allowed_dependencies",
            ),
            (
                "parser_localcat_codec",
                "import parser_registry\n",
                "localcat_codec.allowed_dependencies",
            ),
            (
                "parser_tmx_codec",
                "import resource_importer\n",
                "tmx_codec.allowed_dependencies",
            ),
        )
        for module_name, source, rule_id in cases:
            with self.subTest(module=module_name, source=source):
                self.assertIn(
                    rule_id,
                    {
                        violation.rule_id
                        for violation in self.policy.check_module(
                            SourceModule(module_name, source)
                        )
                    },
                )

        configured = tuple(
            prefix
            for rule in self.policy.allowed_import_rules
            for prefix in rule.importer_prefixes
        )
        self.assertCountEqual(configured, PARSER_MODULE_PREFIXES)


class SyntaxAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_parser_architecture_policy()

    def test_import_aliased_grammar_call_detects_a_second_parser(self) -> None:
        violations = self.policy.check_module(
            SourceModule(
                "editor_project",
                "import json as payload_json\npayload_json.loads('{}')\n",
            )
        )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "syntax.localcat_or_tm_json_owner")
        self.assertEqual(violations[0].target, "json.loads")

    def test_owner_call_is_allowed_and_string_lookalike_is_ignored(self) -> None:
        owner = self.policy.check_module(
            SourceModule(
                "parser_localcat_codec",
                "from json import loads as decode\ndecode('{}')\n",
            )
        )
        lookalike = self.policy.check_module(
            SourceModule(
                "editor_project",
                "EXAMPLE = 'json.loads'\njson.loads('{}')\n",
            )
        )

        self.assertEqual(owner, ())
        self.assertEqual(lookalike, ())

    def test_unrelated_json_user_is_outside_the_migration_inventory(self) -> None:
        violations = self.policy.check_module(
            SourceModule(
                "workspace_preferences",
                "import json\njson.loads('{}')\n",
            )
        )

        self.assertEqual(violations, ())

    def test_dotted_import_uses_python_root_binding_for_grammar_calls(self) -> None:
        violations = self.policy.check_module(
            SourceModule(
                "editor_project",
                "import json.decoder\njson.loads('{}')\n",
            )
        )

        self.assertEqual(
            [violation.rule_id for violation in violations],
            ["syntax.localcat_or_tm_json_owner"],
        )

    def test_relevant_star_import_fails_closed_only_in_migration_inventory(self) -> None:
        legacy = self.policy.check_module(
            SourceModule("editor_project", "from json import *\nloads('{}')\n")
        )
        unrelated = self.policy.check_module(
            SourceModule("workspace_preferences", "from json import *\nloads('{}')\n")
        )

        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0].mechanism, "star_import")
        self.assertEqual(unrelated, ())


class DeferredBoundaryMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_parser_architecture_policy()

    def test_every_deferred_owner_has_a_live_negative_import_guard(self) -> None:
        self.assertEqual(
            {boundary.boundary_id for boundary in DEFERRED_BOUNDARY_MATRIX},
            {
                "rpy_plugin_implementation",
                "workspace_authority",
                "multi_document_aggregation",
                "project_package_reconciliation",
                "collaborative_chunks",
                "cross_device_sync",
                "tm_storage_authority",
                "tmx_context_interchange",
                "speaker_profiles",
                "automatic_termbase_column_inference",
                "other_deferred_format_codecs",
            },
        )
        for boundary in DEFERRED_BOUNDARY_MATRIX:
            with self.subTest(boundary=boundary.boundary_id):
                target = boundary.forbidden_module_prefixes[0]
                violations = self.policy.check_module(
                    SourceModule("parser_localcat_codec", f"import {target}\n")
                )
                self.assertIn(
                    f"deferred.{boundary.boundary_id}",
                    {violation.rule_id for violation in violations},
                )

    def test_deferred_symbols_require_their_exact_owner_module(self) -> None:
        foreign = self.policy.check_module(
            SourceModule(
                "parser_localcat_codec",
                "from foreign_contracts import RpyToken, TMStore\n",
            )
        )
        owned = self.policy.check_module(
            SourceModule(
                "parser_localcat_codec",
                "import rpy_tokens as tokens\nvalue = tokens.RpyToken\n",
            )
        )
        self.assertEqual(
            {
                violation.rule_id
                for violation in foreign
                if violation.rule_id.startswith("deferred.")
            },
            set(),
        )
        self.assertIn(
            "deferred.rpy_plugin_implementation",
            {violation.rule_id for violation in owned},
        )

    def test_exact_or_child_matching_catches_children_without_prefix_false_positives(self) -> None:
        allowed = self.policy.check_module(
            SourceModule(
                "parser_tmx_codec",
                "import tm_storehouse\nimport workspace_tools\nimport rpy_project_codecs\n",
            )
        )
        forbidden = self.policy.check_module(
            SourceModule(
                "parser_tmx_codec",
                "import tm_store.internal\nimport workspace.state\n",
            )
        )

        self.assertEqual(
            {
                violation.rule_id
                for violation in allowed
                if violation.rule_id.startswith("deferred.")
            },
            set(),
        )
        self.assertEqual(
            {violation.rule_id for violation in forbidden},
            {
                "tmx_codec.allowed_dependencies",
                "deferred.tm_storage_authority",
                "deferred.workspace_authority",
            },
        )
        self.assertTrue(module_matches_prefix("workspace.state", "workspace"))
        self.assertFalse(module_matches_prefix("workspace_tools", "workspace"))


class ConfigurationDeterminismTests(unittest.TestCase):
    def test_invalid_prefix_and_duplicate_prefix_are_rejected(self) -> None:
        with self.assertRaisesRegex(ArchitectureConfigurationError, "dotted Python name"):
            ForbiddenImportRule("bad", ("parser_*",), ("tm_engine",), "reason")
        with self.assertRaisesRegex(ArchitectureConfigurationError, "duplicate prefixes"):
            ForbiddenImportRule(
                "bad",
                ("parser_contracts", "parser_contracts"),
                ("tm_engine",),
                "reason",
            )

    def test_duplicate_rule_id_and_definition_are_rejected_deterministically(self) -> None:
        first = ForbiddenImportRule("one", ("parser_source",), ("tm_engine",), "reason")
        same_id = ForbiddenImportRule("one", ("parser_source",), ("tm_store",), "reason")
        same_definition = ForbiddenImportRule(
            "two",
            ("parser_source",),
            ("tm_engine",),
            "reason",
        )
        allow_source = AllowedImportRule(
            "allow.source",
            ("parser_source",),
            (),
            (),
            "reason",
        )

        with self.assertRaisesRegex(ArchitectureConfigurationError, "duplicate rule id: one"):
            ArchitecturePolicy(
                ("parser_source",),
                (allow_source,),
                (first, same_id),
                (),
                (),
            )
        with self.assertRaisesRegex(
            ArchitectureConfigurationError,
            "duplicate rule definition: two",
        ):
            ArchitecturePolicy(
                ("parser_source",),
                (allow_source,),
                (first, same_definition),
                (),
                (),
            )

    def test_duplicate_source_modules_are_rejected_and_findings_are_sorted(self) -> None:
        policy = ArchitecturePolicy(
            ("parser_contracts",),
            (
                AllowedImportRule(
                    "allow.contracts",
                    ("parser_contracts",),
                    ("workspace", "tm_engine", "tm_store"),
                    (),
                    "reason",
                ),
            ),
            (
                ForbiddenImportRule(
                    "forbidden",
                    ("parser_contracts",),
                    ("tm_engine", "tm_store"),
                    "reason",
                ),
            ),
            (),
            (
                DeferredBoundary(
                    "future",
                    ("workspace",),
                    (),
                    "future-spec",
                ),
            ),
        )
        with self.assertRaisesRegex(ArchitectureConfigurationError, "duplicate source modules"):
            policy.check_modules(
                (
                    SourceModule("parser_contracts", ""),
                    SourceModule("parser_contracts", ""),
                )
            )

        findings = policy.check_module(
            SourceModule(
                "parser_contracts",
                "import workspace\nimport tm_store\nimport tm_engine\n",
            )
        )
        self.assertEqual(
            [(item.line, item.rule_id, item.target) for item in findings],
            [
                (1, "deferred.future", "workspace"),
                (2, "forbidden", "tm_store"),
                (3, "forbidden", "tm_engine"),
            ],
        )

    def test_exclusive_call_rule_rejects_ambiguous_configuration(self) -> None:
        with self.assertRaisesRegex(ArchitectureConfigurationError, "non-empty tuple"):
            ExclusiveCallRule(
                "bad",
                (),
                ("editor_project",),
                ("parser_json_support",),
                "reason",
            )

    def test_allowed_import_rule_rejects_ambiguous_configuration(self) -> None:
        with self.assertRaisesRegex(ArchitectureConfigurationError, "must be a tuple"):
            AllowedImportRule(
                "bad",
                ("parser_source",),
                [],  # type: ignore[arg-type]
                (),
                "reason",
            )

        first = AllowedImportRule("one", ("parser",), (), (), "reason")
        overlapping = AllowedImportRule(
            "two",
            ("parser.source",),
            (),
            (),
            "reason",
        )
        with self.assertRaisesRegex(
            ArchitectureConfigurationError,
            "overlapping allowed-import owners",
        ):
            ArchitecturePolicy(
                ("parser",),
                (first, overlapping),
                (),
                (),
                (),
            )

    def test_parser_allowlist_owner_coverage_rejects_missing_and_extra(self) -> None:
        with self.assertRaisesRegex(
            ArchitectureConfigurationError,
            "coverage mismatch.*missing: parser_source",
        ):
            ArchitecturePolicy(("parser_source",), (), (), (), ())

        extra = AllowedImportRule(
            "extra",
            ("parser_extra",),
            (),
            (),
            "reason",
        )
        with self.assertRaisesRegex(
            ArchitectureConfigurationError,
            "missing: parser_source; extra: parser_extra",
        ):
            ArchitecturePolicy(("parser_source",), (extra,), (), (), ())


if __name__ == "__main__":
    unittest.main()
