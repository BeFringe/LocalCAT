"""Wave 4 final architecture and deferred-boundary acceptance."""

from __future__ import annotations

import ast
from collections import Counter
import importlib.util
from pathlib import Path
import unittest

from tests.parser_architecture_test_support import (
    APPLICATION_FACADE_PREFIXES,
    DEFERRED_BOUNDARY_MATRIX,
    ENGINE_STORE_PREFIXES,
    PARSER_CODEC_PREFIXES,
    PARSER_MODULE_PREFIXES,
    SourceModule,
    build_parser_architecture_policy,
    collect_import_references,
    module_matches_prefix,
)


_ROOT = Path(__file__).resolve().parent.parent

_EXPECTED_PARSER_MODULES = frozenset(
    {
        "parser_contracts",
        "parser_source",
        "parser_registry",
        "parser_composition",
        "parser_json_support",
        "parser_xlsx_support",
        "parser_localcat_codec",
        "parser_gettext_codec",
        "parser_tmx_codec",
        "parser_tm_json_codec",
        "parser_termbase_codec",
    }
)
_EXPECTED_ENGINE_STORE_PREFIXES = frozenset(
    {
        "tm_engine",
        "glossary_engine",
        "tm_sqlite_store",
        "tm_store",
        "termbase_store",
    }
)
_EXPECTED_ENGINE_STORE_MODULES = frozenset(
    {
        "tm_engine",
        "glossary_engine",
        "tm_sqlite_store",
        "termbase_store",
    }
)
_EXPECTED_MIGRATED_APPLICATION_FACADES = frozenset(
    {
        "editor_project",
        "resource_importer",
        "tm_json_importer",
        "logic_controller",
        "translation_runner",
        "stress_runner",
    }
)
_EXPECTED_DIRECT_SURFACE_CONSUMERS = frozenset(
    {
        "editor_project",
        "editor_project_workspace_adapter",
        "project_workspace_intake",
        "resource_importer",
        "tm_json_importer",
        "logic_controller",
    }
)

_MIGRATED_GRAMMAR_CALLS = frozenset(
    {
        "json.load",
        "json.loads",
        "json.dump",
        "json.dumps",
        "json.JSONDecoder.decode",
        "json.JSONDecoder.raw_decode",
        "csv.reader",
        "csv.DictReader",
        "openpyxl.load_workbook",
        "xml.etree.ElementTree.fromstring",
        "xml.etree.ElementTree.iterparse",
        "xml.etree.ElementTree.parse",
        "xml.parsers.expat.ParserCreate",
    }
)
_APPLICATION_FACTORY_ONLY_CALLS = frozenset(
    {
        "parser_composition.ParserApplicationSurface",
        "parser_composition.OpenedParserInput",
        "parser_composition.PreparedCanonicalWrite",
        "parser_composition.ParserRegistry",
        "parser_composition.CanonicalBytes",
        "parser_composition.GuardedParseSession",
        "parser_composition.SealedSourceSnapshot",
        "parser_composition.create_sealed_snapshot",
        "parser_composition.atomic_write_bytes",
        "parser_composition.validate",
        "parser_composition.materialize",
    }
)
_FACADE_CALL_INVENTORY = {
    "editor_project": (),
    # Existing target-side JSONL merge/render policy remains Application-owned;
    # it is not TMX input grammar.
    "resource_importer": (("json.dumps", 1), ("json.loads", 1)),
    # JSONL rendering remains the CLI's output policy, not single-input parsing.
    "tm_json_importer": (("json.dumps", 1),),
    "logic_controller": (),
    "translation_runner": (),
    "stress_runner": (),
}

# JSON/XML/CSV/XLSX calls outside the migrated Parser inputs have independent
# owners (TM persistence, evidence readers, launch/config adapters, and tools).
# Keep that exemption closed: a new production module which starts using a
# migrated grammar primitive must be classified here or rejected by Wave 4.
_KNOWN_NON_PARSER_GRAMMAR_MODULES = frozenset(
    {
        "backend_scaling_gate",
        "backend_throughput_harness",
        "capability_host",
        "collaborative_chunk_contracts",
        "collaborative_chunk_store",
        "deterministic_workload",
        "editor_contracts",
        "editor_controller",
        "excel_adapter_openpyxl",
        "generate_comparative_report",
        "generate_decision_memo",
        "macos_app_launcher",
        "matcher_capability",
        "project_package",
        "qt_editor",
        "renpy_tm_compat",
        "resource_repository",
        "resource_package_contracts",
        "resource_receipt_ledger",
        "termbase_store",
        "tm_activation_journal",
        "tm_activation_recovery",
        "tm_benchmark",
        "tm_benchmark_gate",
        "tm_benchmark_latency",
        "tm_benchmark_oracle",
        "tm_benchmark_process",
        "tm_benchmark_query_process",
        "tm_content_attestation",
        "tm_contracts",
        "tm_engine",
        "tm_gate_a",
        "tm_gate_b",
        "tm_migration",
        "tm_retrieval_capability",
        "tm_retrieval_validation",
        "tm_snapshot_artifacts",
        "tm_sqlite_candidate_projection",
        "tm_sqlite_store",
        "tm_stage_sealer",
        "tools.validate_tm_acceptance_matrix",
        "tools.validate_tm_fault_matrix",
        "tools.validate_tm_release_criteria",
        "tools.validate_tm_release_evidence",
        "tools.generate_multi_document_current_source_evidence",
        "tools.generate_collaborative_chunks_current_source_evidence",
        "tools.generate_language_resource_portability_current_source_evidence",
        "validate_benchmark_contract",
        "validate_decision_memo",
        "workspace_state",
    }
)

_EXPECTED_PARSER_GRAMMAR_MODULES = frozenset(
    {
        "parser_json_support",
        "parser_localcat_codec",
        "parser_termbase_codec",
        "parser_tmx_codec",
        "parser_xlsx_support",
    }
)

_EXPECTED_NONLITERAL_DYNAMIC_IMPORT_INVENTORY = {
    # Capability Host owns two existing runtime-selected validation anchors;
    # Parser production has no computed dynamic import target.
    ("capability_host", "importlib.import_module"): 2,
}

_EXPECTED_DEFERRED_OWNERS = {
    "rpy_plugin_implementation": "rpy-project-codec",
    "workspace_authority": "multi-document-project-workspace",
    "multi_document_aggregation": "multi-document-project-workspace",
    "project_package_reconciliation": "multi-document-project-workspace",
    "collaborative_chunks": "collaborative-job-chunks",
    "cross_device_sync": "cross-device-sync-plugin",
    "tm_storage_authority": "tm-storage-retrieval-index",
    "tmx_context_interchange": "tmx-context-interchange",
    "speaker_profiles": "speaker-display-profiles",
    "automatic_termbase_column_inference": (
        "future termbase column-selection UI contract"
    ),
    "other_deferred_format_codecs": "owning future format specs",
}


def _production_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in _ROOT.rglob("*.py"):
        relative = path.relative_to(_ROOT)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.name.startswith("test_"):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def _module_name(path: Path) -> str:
    relative = path.relative_to(_ROOT).with_suffix("")
    return ".".join(relative.parts)


def _production_modules() -> dict[str, SourceModule]:
    return {
        _module_name(path): SourceModule(
            _module_name(path),
            path.read_text(encoding="utf-8"),
        )
        for path in _production_paths()
    }


def _parser_modules(modules: dict[str, SourceModule]) -> dict[str, SourceModule]:
    return {
        name: module
        for name, module in modules.items()
        if name in PARSER_MODULE_PREFIXES
    }


def _wave4_synthetic_violations(source: str):
    return build_parser_architecture_policy().check_module(
        SourceModule("parser_contracts", source)
    )


def _qualified_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _import_from_base(module_name: str, node: ast.ImportFrom) -> str:
    if not node.level:
        if node.module is None:
            raise AssertionError("absolute from-import has no module")
        return node.module
    package = module_name.rpartition(".")[0]
    if not package:
        raise AssertionError("top-level production modules cannot use relative imports")
    relative = "." * node.level + (node.module or "")
    return importlib.util.resolve_name(relative, package)


def _resolve_binding(raw_name: str, bindings: dict[str, str]) -> str | None:
    matching_prefixes = tuple(
        bound_name
        for bound_name in bindings
        if raw_name == bound_name or raw_name.startswith(f"{bound_name}.")
    )
    if not matching_prefixes:
        return None
    prefix = max(matching_prefixes, key=len)
    return f"{bindings[prefix]}{raw_name[len(prefix):]}"


def _canonical_call_owner(resolved: str) -> str:
    """Collapse stdlib-qualified spellings of the same managed primitive."""

    decoder_prefix = "json.decoder.JSONDecoder."
    if resolved.startswith(decoder_prefix):
        return f"json.JSONDecoder.{resolved.removeprefix(decoder_prefix)}"
    return resolved


def _literal_dynamic_module_binding(
    node: ast.AST,
    *,
    bindings: dict[str, str],
    module_name: str,
) -> str | None:
    """Resolve the module object returned by a literal dynamic import call."""

    if not isinstance(node, ast.Call) or not node.args:
        return None
    raw_name = _qualified_name(node.func)
    if raw_name is None:
        return None
    resolved = _resolve_binding(raw_name, bindings)
    if resolved not in {"builtins.__import__", "importlib.import_module"}:
        return None
    argument = node.args[0]
    if not isinstance(argument, ast.Constant) or type(argument.value) is not str:
        return None
    target = argument.value
    if not target:
        return None
    if target.startswith("."):
        package = module_name.rpartition(".")[0]
        if not package:
            return None
        try:
            return importlib.util.resolve_name(target, package)
        except (ImportError, ValueError):
            return None
    return target


def _resolved_expression_name(
    node: ast.AST,
    *,
    bindings: dict[str, str],
    module_name: str,
) -> str | None:
    """Resolve the approved import/alias/call-result expression subset."""

    raw_name = _qualified_name(node)
    if raw_name is not None:
        resolved = _resolve_binding(raw_name, bindings)
        if resolved is not None:
            return resolved
    if isinstance(node, ast.Attribute):
        owner = _resolved_expression_name(
            node.value,
            bindings=bindings,
            module_name=module_name,
        )
        return None if owner is None else f"{owner}.{node.attr}"
    if isinstance(node, ast.Call):
        dynamic_module = _literal_dynamic_module_binding(
            node,
            bindings=bindings,
            module_name=module_name,
        )
        if dynamic_module is not None:
            return dynamic_module
        # This deliberately tracks only the imported callable identity.  It is
        # enough for controlled forms such as JSONDecoder().decode without
        # pretending to infer arbitrary Python return types.
        return _resolved_expression_name(
            node.func,
            bindings=bindings,
            module_name=module_name,
        )
    return None


def _assignment_target_values(
    target: ast.AST,
    value: ast.AST,
) -> tuple[tuple[str, ast.AST], ...]:
    """Flatten simple name/attribute and fixed tuple/list assignments."""

    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(
        value,
        (ast.Tuple, ast.List),
    ):
        if len(target.elts) != len(value.elts) or any(
            isinstance(item, ast.Starred) for item in target.elts
        ):
            return ()
        return tuple(
            pair
            for target_item, value_item in zip(target.elts, value.elts)
            for pair in _assignment_target_values(target_item, value_item)
        )
    if isinstance(target, ast.Name):
        return ((target.id, value),)
    if isinstance(target, ast.Attribute):
        name = _qualified_name(target)
        return () if name is None else ((name, value),)
    return ()


def _resolved_bindings(tree: ast.AST, *, module_name: str) -> dict[str, str]:
    """Resolve imports plus simple assignment aliases used to hide a call."""

    bindings: dict[str, str] = {"__import__": "builtins.__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.partition(".")[0]
                    bindings[root] = root
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(module_name, node)
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = f"{base}.{alias.name}"

    # A fixed point also catches ``again = alias`` while remaining deliberately
    # narrower than general Python data-flow analysis.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = (node.target,)
                value = node.value
            else:
                continue
            for target in targets:
                for target_name, target_value in _assignment_target_values(
                    target,
                    value,
                ):
                    resolved = _resolved_expression_name(
                        target_value,
                        bindings=bindings,
                        module_name=module_name,
                    )
                    if resolved is None or target_name in bindings:
                        continue
                    bindings[target_name] = resolved
                    changed = True
    return bindings


def _resolved_imported_calls(
    source: str,
    *,
    module_name: str,
) -> tuple[tuple[str, int, int], ...]:
    tree = ast.parse(source, filename=f"<{module_name}>")
    bindings = _resolved_bindings(tree, module_name=module_name)
    calls: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolved_expression_name(
            node.func,
            bindings=bindings,
            module_name=module_name,
        )
        if resolved is not None:
            calls.add(
                (
                    _canonical_call_owner(resolved),
                    node.lineno,
                    node.col_offset,
                )
            )
    return tuple(sorted(calls, key=lambda item: (item[1], item[2], item[0])))


def _literal_dynamic_imports(
    source: str,
    *,
    module_name: str,
) -> tuple[tuple[str, int, int], ...]:
    tree = ast.parse(source, filename=f"<{module_name}>")
    bindings = _resolved_bindings(tree, module_name=module_name)
    references: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        raw_name = _qualified_name(node.func)
        if raw_name is None:
            continue
        resolved = _resolve_binding(raw_name, bindings)
        if resolved not in {"builtins.__import__", "importlib.import_module"}:
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant) or type(argument.value) is not str:
            continue
        target = argument.value
        if not target or target.startswith("."):
            continue
        references.add((target, node.lineno, node.col_offset))
    return tuple(sorted(references, key=lambda item: (item[1], item[2], item[0])))


def _nonliteral_dynamic_import_calls(
    source: str,
    *,
    module_name: str,
) -> tuple[tuple[str, int, int], ...]:
    """Return computed dynamic-import targets; checked production fails closed."""

    tree = ast.parse(source, filename=f"<{module_name}>")
    bindings = _resolved_bindings(tree, module_name=module_name)
    calls: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw_name = _qualified_name(node.func)
        if raw_name is None:
            continue
        resolved = _resolve_binding(raw_name, bindings)
        if resolved not in {"builtins.__import__", "importlib.import_module"}:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and type(
            node.args[0].value
        ) is str:
            continue
        calls.add((resolved, node.lineno, node.col_offset))
    return tuple(sorted(calls, key=lambda item: (item[1], item[2], item[0])))


def _literal_import_policy_rules(module: SourceModule) -> set[str]:
    """Apply the real policy to imports hidden behind literal dynamic calls."""

    policy = build_parser_architecture_policy()
    rules: set[str] = set()
    for target, _line, _column in _literal_dynamic_imports(
        module.source,
        module_name=module.name,
    ):
        synthetic = SourceModule(module.name, f"import {target}\n")
        rules.update(item.rule_id for item in policy.check_module(synthetic))
    return rules


def _deferred_symbol_hits(
    source: str,
    *,
    module_name: str,
    symbol_pairs: tuple[tuple[str, str], ...],
) -> set[tuple[str, str]]:
    """Check symbol pairs without consulting their module-prefix rules."""

    tree = ast.parse(source, filename=f"<{module_name}>")
    bindings = _resolved_bindings(tree, module_name=module_name)
    resolved_names = set(bindings.values())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        resolved = _resolved_expression_name(
            node,
            bindings=bindings,
            module_name=module_name,
        )
        if resolved is not None:
            resolved_names.add(resolved)
    return {
        (owner, symbol)
        for owner, symbol in symbol_pairs
        if f"{owner}.{symbol}" in resolved_names
    }


def _call_owners(
    modules: dict[str, SourceModule],
    qualified_calls: set[str],
) -> set[str]:
    owners: set[str] = set()
    for name, module in modules.items():
        if any(
            call in qualified_calls
            for call, _line, _column in _resolved_imported_calls(
                module.source,
                module_name=name,
            )
        ):
            owners.add(name)
    return owners


def _module_import_targets(module: SourceModule) -> set[str]:
    targets = {
        reference.target
        for reference in collect_import_references(
            module.source,
            module_name=module.name,
        )
    }
    targets.update(
        target
        for target, _line, _column in _literal_dynamic_imports(
            module.source,
            module_name=module.name,
        )
    )
    return targets


def _factory_only_surface_call_hits(
    modules: dict[str, SourceModule],
) -> set[tuple[str, str]]:
    return {
        (name, call)
        for name, module in modules.items()
        if name != "parser_composition"
        for call, _line, _column in _resolved_imported_calls(
            module.source,
            module_name=name,
        )
        if call in _APPLICATION_FACTORY_ONLY_CALLS
    }


def _unclassified_grammar_modules(
    modules: dict[str, SourceModule],
) -> set[str]:
    owners = _call_owners(modules, set(_MIGRATED_GRAMMAR_CALLS))
    classified = {
        *_EXPECTED_PARSER_GRAMMAR_MODULES,
        *_FACADE_CALL_INVENTORY,
        *_KNOWN_NON_PARSER_GRAMMAR_MODULES,
    }
    return owners - classified


def _matches_reserved_engine_store_family(module_name: str) -> bool:
    """Match fixed Engine/Store namespaces plus top-level helper suffixes."""

    return any(
        module_matches_prefix(module_name, prefix)
        or module_name.startswith(f"{prefix}_")
        for prefix in _EXPECTED_ENGINE_STORE_PREFIXES
    )


def _engine_store_modules(
    modules: dict[str, SourceModule],
) -> dict[str, SourceModule]:
    return {
        name: module
        for name, module in modules.items()
        if _matches_reserved_engine_store_family(name)
    }


def _engine_store_reverse_parser_imports(
    modules: dict[str, SourceModule],
) -> set[tuple[str, str]]:
    return {
        (name, target)
        for name, module in _engine_store_modules(modules).items()
        for target in _module_import_targets(module)
        if any(
            module_matches_prefix(target, prefix)
            for prefix in _EXPECTED_PARSER_MODULES
        )
    }


def _local_factory_attribute_call_owners(
    modules: dict[str, SourceModule],
    *,
    factory_name: str,
    attribute_name: str,
) -> set[str]:
    """Bind an attribute call only to a named local factory result."""

    owners: set[str] = set()
    for name, module in modules.items():
        tree = ast.parse(module.source, filename=f"<{name}>")
        factory_results: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if (
                value is None
                or not isinstance(value, ast.Call)
                or not isinstance(value.func, ast.Name)
                or value.func.id != factory_name
            ):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            factory_results.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute_name
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in factory_results
            for node in ast.walk(tree)
        ):
            owners.add(name)
    return owners


def _gettext_grammar_owners(modules: dict[str, SourceModule]) -> set[str]:
    owners: set[str] = set()
    for name, module in modules.items():
        imported_compile_locations = {
            (line, column)
            for call, line, column in _resolved_imported_calls(
                module.source,
                module_name=name,
            )
            if call == "re.compile"
        }
        tree = ast.parse(module.source, filename=f"<{name}>")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (node.lineno, node.col_offset) not in imported_compile_locations:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            pattern = node.args[0].value
            if type(pattern) is str and {"msgid", "msgstr"}.issubset(
                set(pattern.replace("|", " ").replace("(", " ").split())
            ):
                owners.add(name)
            elif type(pattern) is str and "msgid" in pattern and "msgstr" in pattern:
                owners.add(name)
    return owners


def _defined_class_names(modules: dict[str, SourceModule]) -> dict[str, set[str]]:
    return {
        name: {
            node.name
            for node in ast.walk(ast.parse(module.source, filename=f"<{name}>"))
            if isinstance(node, ast.ClassDef)
        }
        for name, module in modules.items()
    }


class Wave4ArchitectureGuardSelfTests(unittest.TestCase):
    def test_structural_guard_ignores_comments_and_strings_but_rejects_real_import(self) -> None:
        harmless = '"""import sync_provider; class BaseParser: pass"""\n# import rpy_project_codec\n'
        violating = "import sync_provider\n"

        self.assertEqual(_wave4_synthetic_violations(harmless), ())
        self.assertTrue(_wave4_synthetic_violations(violating))

    def test_grammar_calls_follow_import_and_assignment_aliases(self) -> None:
        modules = {
            "direct_alias": SourceModule(
                "direct_alias",
                "import json\ndecode = json.loads\ndecode('{}')\n",
            ),
            "from_alias": SourceModule(
                "from_alias",
                "from xml.parsers.expat import ParserCreate as make\nmake()\n",
            ),
            "lookalike": SourceModule(
                "lookalike",
                "class Helper:\n"
                "    def ParserCreate(self):\n"
                "        return None\n"
                "helper = Helper()\nhelper.ParserCreate()\n",
            ),
        }
        self.assertEqual(
            _call_owners(modules, {"json.loads"}),
            {"direct_alias"},
        )
        self.assertEqual(
            _call_owners(modules, {"xml.parsers.expat.ParserCreate"}),
            {"from_alias"},
        )

    def test_literal_dynamic_module_results_keep_their_grammar_owner(self) -> None:
        modules = {
            "json_dynamic": SourceModule(
                "json_dynamic",
                "loader = __import__\n"
                "module = loader('json')\n"
                "decode = module.loads\n"
                "decode('{}')\n",
            ),
            "expat_dynamic": SourceModule(
                "expat_dynamic",
                "import importlib as il\n"
                "load = il.import_module\n"
                "module = load('xml.parsers.expat')\n"
                "make = module.ParserCreate\n"
                "alias = make\n"
                "alias()\n",
            ),
            "json_direct_result": SourceModule(
                "json_direct_result",
                "__import__('json').loads('{}')\n",
            ),
            "expat_direct_result": SourceModule(
                "expat_direct_result",
                "import importlib\n"
                "importlib.import_module('xml.parsers.expat').ParserCreate()\n",
            ),
        }
        self.assertEqual(
            _call_owners(modules, {"json.loads"}),
            {"json_dynamic", "json_direct_result"},
        )
        self.assertEqual(
            _call_owners(modules, {"xml.parsers.expat.ParserCreate"}),
            {"expat_dynamic", "expat_direct_result"},
        )

    def test_factory_only_surface_calls_follow_one_and_two_hop_aliases(self) -> None:
        modules = {
            "one_hop": SourceModule(
                "one_hop",
                "from parser_composition import OpenedParserInput\n"
                "owner = OpenedParserInput\n"
                "owner()\n",
            ),
            "two_hop": SourceModule(
                "two_hop",
                "from parser_composition import OpenedParserInput as opened\n"
                "owner = opened\n"
                "again = owner\n"
                "again()\n",
            ),
            "tuple_and_list": SourceModule(
                "tuple_and_list",
                "from parser_composition import OpenedParserInput\n"
                "[first] = [OpenedParserInput]\n"
                "(second,) = (first,)\n"
                "second()\n",
            ),
            "attribute_alias": SourceModule(
                "attribute_alias",
                "from parser_composition import OpenedParserInput\n"
                "box.owner = OpenedParserInput\n"
                "box.owner()\n",
            ),
            "direct_module_result": SourceModule(
                "direct_module_result",
                "import importlib\n"
                "importlib.import_module('parser_composition').OpenedParserInput()\n",
            ),
        }
        self.assertEqual(
            _factory_only_surface_call_hits(modules),
            {
                ("one_hop", "parser_composition.OpenedParserInput"),
                ("two_hop", "parser_composition.OpenedParserInput"),
                ("tuple_and_list", "parser_composition.OpenedParserInput"),
                ("attribute_alias", "parser_composition.OpenedParserInput"),
                ("direct_module_result", "parser_composition.OpenedParserInput"),
            },
        )

    def test_managed_grammar_variants_and_computed_imports_fail_closed(self) -> None:
        variants = {
            "decoder": SourceModule(
                "decoder",
                "import json\ndecoder = json.JSONDecoder()\ndecoder.decode('{}')\n",
            ),
            "decoder_module_tuple_attribute": SourceModule(
                "decoder_module_tuple_attribute",
                "import json\n"
                "[(box.decoder,)] = [(json.decoder.JSONDecoder(),)]\n"
                "(method,) = (box.decoder.decode,)\n"
                "method('{}')\n",
            ),
            "raw_decoder_module_tuple": SourceModule(
                "raw_decoder_module_tuple",
                "from json import decoder\n"
                "[instance] = [decoder.JSONDecoder()]\n"
                "(method,) = (instance.raw_decode,)\n"
                "method('{}')\n",
            ),
            "dict_reader": SourceModule(
                "dict_reader",
                "from csv import DictReader as rows\nrows([])\n",
            ),
        }
        self.assertEqual(
            _call_owners(variants, {"json.JSONDecoder.decode"}),
            {"decoder", "decoder_module_tuple_attribute"},
        )
        self.assertEqual(
            _call_owners(variants, {"json.JSONDecoder.raw_decode"}),
            {"raw_decoder_module_tuple"},
        )
        self.assertEqual(
            _call_owners(variants, {"csv.DictReader"}),
            {"dict_reader"},
        )
        self.assertEqual(
            _unclassified_grammar_modules(variants),
            {
                "decoder",
                "decoder_module_tuple_attribute",
                "raw_decoder_module_tuple",
                "dict_reader",
            },
        )

        computed = SourceModule(
            "computed_loader",
            "import importlib\n"
            "first, second = (importlib.import_module, importlib.import_module)\n"
            "module_name = 'json'\n"
            "second(module_name)\n",
        )
        self.assertEqual(
            tuple(
                loader
                for loader, _line, _column in _nonliteral_dynamic_import_calls(
                    computed.source,
                    module_name=computed.name,
                )
            ),
            ("importlib.import_module",),
        )

        nested_computed = SourceModule(
            "nested_computed_loader",
            "import importlib\n"
            "[(box.module,)] = [(importlib,)]\n"
            "target = 'parser_' + 'contracts'\n"
            "box.module.import_module(target)\n",
        )
        self.assertEqual(
            tuple(
                loader
                for loader, _line, _column in _nonliteral_dynamic_import_calls(
                    nested_computed.source,
                    module_name=nested_computed.name,
                )
            ),
            ("importlib.import_module",),
        )

    def test_unknown_grammar_module_and_engine_store_helper_fail_closed(self) -> None:
        shadow = {
            "shadow_grammar": SourceModule(
                "shadow_grammar",
                "import json\nparse = json.loads\nparse('{}')\n",
            )
        }
        self.assertEqual(_unclassified_grammar_modules(shadow), {"shadow_grammar"})
        reverse_import = {
            "tm_store_helper": SourceModule(
                "tm_store_helper",
                "loader = __import__\nparser = loader('parser_contracts')\n",
            )
        }
        self.assertEqual(
            set(_engine_store_modules(reverse_import)),
            {"tm_store_helper"},
        )
        self.assertEqual(
            _engine_store_reverse_parser_imports(reverse_import),
            {("tm_store_helper", "parser_contracts")},
        )

        nested_reverse_import = {
            "tm_store_helper": SourceModule(
                "tm_store_helper",
                "import importlib\n"
                "[(box.module,)] = [(importlib,)]\n"
                "box.module.import_module('parser_contracts')\n",
            )
        }
        self.assertEqual(
            _engine_store_reverse_parser_imports(nested_reverse_import),
            {("tm_store_helper", "parser_contracts")},
        )

    def test_literal_dynamic_imports_cannot_bypass_dependency_or_deferred_policy(
        self,
    ) -> None:
        dependency = SourceModule(
            "parser_contracts",
            "loader = __import__\nloader('tm_engine')\n",
        )
        deferred = SourceModule(
            "parser_contracts",
            "import importlib as il\nload = il.import_module\nload('sync_provider')\n",
        )
        harmless = SourceModule(
            "parser_contracts",
            '"__import__(\\"sync_provider\\")"\n# __import__("tm_engine")\n',
        )

        self.assertIn(
            "contracts.allowed_dependencies",
            _literal_import_policy_rules(dependency),
        )
        self.assertIn(
            "deferred.cross_device_sync",
            _literal_import_policy_rules(deferred),
        )
        self.assertEqual(_literal_import_policy_rules(harmless), set())

    def test_deferred_symbol_probe_is_independent_of_module_prefix_matching(
        self,
    ) -> None:
        pairs = (("otherwise_allowed", "FutureAuthority"),)
        direct = "from otherwise_allowed import FutureAuthority as authority\n"
        attribute = "import otherwise_allowed as allowed\nvalue = allowed.FutureAuthority\n"
        lookalike = "class FutureAuthority:\n    pass\n"

        self.assertEqual(
            _deferred_symbol_hits(
                direct,
                module_name="parser_contracts",
                symbol_pairs=pairs,
            ),
            set(pairs),
        )
        self.assertEqual(
            _deferred_symbol_hits(
                attribute,
                module_name="parser_contracts",
                symbol_pairs=pairs,
            ),
            set(pairs),
        )
        self.assertEqual(
            _deferred_symbol_hits(
                lookalike,
                module_name="parser_contracts",
                symbol_pairs=pairs,
            ),
            set(),
        )


class Wave4ProductionArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = _production_modules()
        cls.parsers = _parser_modules(cls.modules)

    def test_parser_module_inventory_is_closed_and_policy_scans_the_real_tree(self) -> None:
        self.assertEqual(set(PARSER_MODULE_PREFIXES), _EXPECTED_PARSER_MODULES)
        actual_parser_files = {
            name for name in self.modules if name.startswith("parser_")
        }
        self.assertEqual(actual_parser_files, _EXPECTED_PARSER_MODULES)
        self.assertEqual(set(self.parsers), _EXPECTED_PARSER_MODULES)
        self.assertEqual(
            set(APPLICATION_FACADE_PREFIXES),
            _EXPECTED_MIGRATED_APPLICATION_FACADES,
        )
        self.assertTrue(_EXPECTED_MIGRATED_APPLICATION_FACADES.issubset(self.modules))

        violations = build_parser_architecture_policy().check_modules(
            self.modules.values()
        )
        self.assertEqual(violations, ())

        literal_policy_violations = {
            (name, rule)
            for name, module in self.modules.items()
            for rule in _literal_import_policy_rules(module)
        }
        self.assertEqual(literal_policy_violations, set())
        nonliteral_dynamic_inventory = Counter(
            (name, loader)
            for name, module in self.modules.items()
            for loader, _line, _column in _nonliteral_dynamic_import_calls(
                module.source,
                module_name=name,
            )
        )
        self.assertEqual(
            dict(sorted(nonliteral_dynamic_inventory.items())),
            _EXPECTED_NONLITERAL_DYNAMIC_IMPORT_INVENTORY,
        )
        self.assertEqual(_factory_only_surface_call_hits(self.modules), set())
        self.assertEqual(_unclassified_grammar_modules(self.modules), set())
        grammar_owners = _call_owners(
            self.modules,
            set(_MIGRATED_GRAMMAR_CALLS),
        )
        self.assertEqual(
            grammar_owners
            - set(_EXPECTED_PARSER_GRAMMAR_MODULES)
            - set(_FACADE_CALL_INVENTORY),
            set(_KNOWN_NON_PARSER_GRAMMAR_MODULES),
        )

    def test_migrated_facade_inventory_and_direct_surface_consumers_are_closed(
        self,
    ) -> None:
        facades = {
            name: self.modules[name]
            for name in _EXPECTED_MIGRATED_APPLICATION_FACADES
        }
        actual_surface_consumers = {
            name
            for name, module in self.modules.items()
            if name not in _EXPECTED_PARSER_MODULES
            and any(
                module_matches_prefix(target, "parser_composition")
                for target in _module_import_targets(module)
            )
        }
        self.assertEqual(
            actual_surface_consumers,
            _EXPECTED_DIRECT_SURFACE_CONSUMERS,
        )
        self.assertEqual(set(_FACADE_CALL_INVENTORY), set(facades))
        for name, module in facades.items():
            observed = Counter(
                call
                for call, _line, _column in _resolved_imported_calls(
                    module.source,
                    module_name=name,
                )
                if call in _MIGRATED_GRAMMAR_CALLS
            )
            with self.subTest(facade=name):
                self.assertEqual(
                    dict(sorted(observed.items())),
                    dict(_FACADE_CALL_INVENTORY[name]),
                )

    def test_composition_is_the_only_builtin_codec_importer(self) -> None:
        owners_by_codec: dict[str, set[str]] = {
            codec: set() for codec in PARSER_CODEC_PREFIXES
        }
        for name, module in self.modules.items():
            for reference in collect_import_references(
                module.source,
                module_name=name,
            ):
                for codec in PARSER_CODEC_PREFIXES:
                    if module_matches_prefix(reference.target, codec):
                        owners_by_codec[codec].add(name)
            for target, _line, _column in _literal_dynamic_imports(
                module.source,
                module_name=name,
            ):
                for codec in PARSER_CODEC_PREFIXES:
                    if module_matches_prefix(target, codec):
                        owners_by_codec[codec].add(name)

        self.assertEqual(
            owners_by_codec,
            {codec: {"parser_composition"} for codec in PARSER_CODEC_PREFIXES},
        )

    def test_each_migrated_format_has_one_parser_grammar_owner(self) -> None:
        self.assertEqual(
            _call_owners(self.parsers, {"json.load", "json.loads"}),
            {"parser_json_support"},
        )
        self.assertEqual(
            _call_owners(self.parsers, {"json.dump", "json.dumps"}),
            {"parser_localcat_codec"},
        )
        self.assertEqual(
            _call_owners(self.parsers, {"csv.reader"}),
            {"parser_termbase_codec"},
        )
        self.assertEqual(
            _local_factory_attribute_call_owners(
                self.parsers,
                factory_name="_openpyxl_module",
                attribute_name="load_workbook",
            ),
            {"parser_termbase_codec"},
        )
        self.assertEqual(
            _call_owners(self.parsers, {"xml.parsers.expat.ParserCreate"}),
            {"parser_tmx_codec", "parser_xlsx_support"},
        )
        self.assertEqual(
            _gettext_grammar_owners(self.parsers),
            {"parser_gettext_codec"},
        )
        self.assertEqual(
            _gettext_grammar_owners(self.modules),
            {"parser_gettext_codec"},
        )

    def test_legacy_facades_have_no_parallel_input_or_writer_grammar(self) -> None:
        editor = {"editor_project": self.modules["editor_project"]}
        normalized_cli = {"tm_json_importer": self.modules["tm_json_importer"]}
        resource = {"resource_importer": self.modules["resource_importer"]}
        glossary = {"glossary_engine": self.modules["glossary_engine"]}

        self.assertEqual(
            _call_owners(
                editor,
                {"json.load", "json.loads", "json.dump", "json.dumps"},
            ),
            set(),
        )
        self.assertEqual(
            _call_owners(normalized_cli, {"json.load", "json.loads"}),
            set(),
        )
        self.assertEqual(
            _call_owners(resource, {"csv.reader", "openpyxl.load_workbook"}),
            set(),
        )
        self.assertEqual(
            _call_owners(
                resource,
                {
                    "xml.etree.ElementTree.fromstring",
                    "xml.etree.ElementTree.iterparse",
                    "xml.etree.ElementTree.parse",
                    "xml.parsers.expat.ParserCreate",
                },
            ),
            set(),
        )
        self.assertEqual(
            _call_owners(glossary, {"csv.reader", "openpyxl.load_workbook"}),
            set(),
        )

        classes = _defined_class_names(self.modules)
        retired = {"BaseParser", "POHandler", "GlossaryLoader"}
        self.assertEqual(
            {
                (module, class_name)
                for module, names in classes.items()
                for class_name in names & retired
            },
            set(),
        )

    def test_parser_and_engine_store_do_not_reverse_import_or_reexport(self) -> None:
        parser_imports = {
            target
            for module in self.parsers.values()
            for target in _module_import_targets(module)
        }
        engine_modules = _engine_store_modules(self.modules)
        self.assertEqual(set(ENGINE_STORE_PREFIXES), _EXPECTED_ENGINE_STORE_PREFIXES)
        self.assertEqual(set(engine_modules), _EXPECTED_ENGINE_STORE_MODULES)
        self.assertEqual(
            _engine_store_reverse_parser_imports(self.modules),
            set(),
        )
        engine_imports = {
            target
            for module in engine_modules.values()
            for target in _module_import_targets(module)
        }

        self.assertFalse(
            any(
                module_matches_prefix(target, prefix)
                for target in parser_imports
                for prefix in ENGINE_STORE_PREFIXES
            )
        )
        self.assertFalse(
            any(
                module_matches_prefix(target, prefix)
                for target in engine_imports
                for prefix in PARSER_MODULE_PREFIXES
            )
        )

    def test_deferred_matrix_is_exact_and_every_import_and_symbol_probe_fails(self) -> None:
        boundaries = {
            boundary.boundary_id: boundary
            for boundary in DEFERRED_BOUNDARY_MATRIX
        }
        self.assertEqual(
            {name: boundary.owner_spec for name, boundary in boundaries.items()},
            _EXPECTED_DEFERRED_OWNERS,
        )

        all_symbol_pairs = tuple(
            pair
            for boundary in boundaries.values()
            for pair in boundary.forbidden_symbol_pairs
        )
        production_symbol_hits = {
            (name, owner, symbol)
            for name, module in self.parsers.items()
            for owner, symbol in _deferred_symbol_hits(
                module.source,
                module_name=name,
                symbol_pairs=all_symbol_pairs,
            )
        }
        self.assertEqual(production_symbol_hits, set())

        policy = build_parser_architecture_policy()
        for boundary_id, boundary in boundaries.items():
            expected_rule = f"deferred.{boundary_id}"
            for prefix in boundary.forbidden_module_prefixes:
                with self.subTest(boundary=boundary_id, module=prefix):
                    violations = policy.check_module(
                        SourceModule("parser_contracts", f"import {prefix}\n")
                    )
                    self.assertIn(expected_rule, {item.rule_id for item in violations})
            for owner, symbol in boundary.forbidden_symbol_pairs:
                with self.subTest(boundary=boundary_id, symbol=f"{owner}.{symbol}"):
                    symbol_hits = _deferred_symbol_hits(
                        f"from {owner} import {symbol}\n",
                        module_name="parser_contracts",
                        symbol_pairs=((owner, symbol),),
                    )
                    self.assertEqual(symbol_hits, {(owner, symbol)})
                    violations = policy.check_module(
                        SourceModule(
                            "parser_contracts",
                            f"from {owner} import {symbol}\n",
                        )
                    )
                    self.assertIn(expected_rule, {item.rule_id for item in violations})

    def test_parser_cannot_import_qt_workspace_tmstore_or_sync_provider_authority(self) -> None:
        authority_targets = {
            "qt_editor": "contracts.allowed_dependencies",
            "editor_controller": "contracts.allowed_dependencies",
            "workspace_state": "deferred.workspace_authority",
            "tm_sqlite_store": "deferred.tm_storage_authority",
            "sync_provider": "deferred.cross_device_sync",
        }
        policy = build_parser_architecture_policy()
        for target, expected_rule in authority_targets.items():
            with self.subTest(target=target):
                violations = policy.check_module(
                    SourceModule("parser_contracts", f"import {target}\n")
                )
                self.assertIn(expected_rule, {item.rule_id for item in violations})

    def test_parser_defines_no_deferred_authority_type_locally(self) -> None:
        forbidden_symbols = {
            symbol
            for boundary in DEFERRED_BOUNDARY_MATRIX
            for _owner, symbol in boundary.forbidden_symbol_pairs
        }
        classes = _defined_class_names(self.parsers)
        self.assertEqual(
            {
                (module, class_name)
                for module, names in classes.items()
                for class_name in names & forbidden_symbols
            },
            set(),
        )

    def test_codec_provider_port_remains_legal_without_sync_provider_authority(self) -> None:
        classes = _defined_class_names(self.parsers)
        self.assertIn("CodecProvider", classes["parser_contracts"])
        self.assertIn("ProviderBinding", classes["parser_composition"])
        parser_import_targets = {
            reference.target
            for module in self.parsers.values()
            for reference in collect_import_references(
                module.source,
                module_name=module.name,
            )
        }
        self.assertFalse(
            any(
                module_matches_prefix(target, prefix)
                for target in parser_import_targets
                for prefix in ("sync_provider", "remote_provider", "cross_device_sync")
            )
        )


if __name__ == "__main__":
    unittest.main()
