"""Reusable AST guards for the Parser dependency and authority boundaries.

This Wave 0 module is test support, not proof about the current production
tree.  It lets later waves feed real module sources into the same deterministic
checks once those modules exist and migration has removed the legacy grammars.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
import importlib.util
import re
import sys
from typing import Iterable


_DOTTED_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


class ArchitectureConfigurationError(ValueError):
    """The architecture rule set is ambiguous or cannot be matched safely."""


class ImportMechanism(str, Enum):
    STATIC = "static"
    LITERAL_IMPORTLIB = "literal_importlib"


@dataclass(frozen=True, slots=True)
class SourceModule:
    name: str
    source: str

    def __post_init__(self) -> None:
        _require_dotted_name(self.name, field="source module name")
        if type(self.source) is not str:
            raise ArchitectureConfigurationError("source must be an exact string")


@dataclass(frozen=True, slots=True)
class ImportReference:
    target: str
    symbol: str | None
    line: int
    column: int
    mechanism: ImportMechanism


@dataclass(frozen=True, slots=True)
class ForbiddenImportRule:
    rule_id: str
    importer_prefixes: tuple[str, ...]
    forbidden_prefixes: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_rule_id(self.rule_id)
        _require_prefixes(self.importer_prefixes, field="importer_prefixes")
        _require_prefixes(self.forbidden_prefixes, field="forbidden_prefixes")
        _require_reason(self.reason)


@dataclass(frozen=True, slots=True)
class AllowedImportRule:
    rule_id: str
    importer_prefixes: tuple[str, ...]
    allowed_prefixes: tuple[str, ...]
    allowed_external_roots: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_rule_id(self.rule_id)
        _require_prefixes(self.importer_prefixes, field="importer_prefixes")
        _require_optional_prefixes(self.allowed_prefixes, field="allowed_prefixes")
        _require_identifiers(
            self.allowed_external_roots,
            field="allowed_external_roots",
        )
        _require_reason(self.reason)


@dataclass(frozen=True, slots=True)
class ExclusiveCallRule:
    """Reserve imported parser API calls for their sole syntax owner(s)."""

    rule_id: str
    qualified_calls: tuple[str, ...]
    checked_importer_prefixes: tuple[str, ...]
    allowed_importer_prefixes: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        _require_rule_id(self.rule_id)
        _require_prefixes(self.qualified_calls, field="qualified_calls")
        _require_prefixes(
            self.checked_importer_prefixes,
            field="checked_importer_prefixes",
        )
        _require_prefixes(
            self.allowed_importer_prefixes,
            field="allowed_importer_prefixes",
        )
        _require_reason(self.reason)


@dataclass(frozen=True, slots=True)
class DeferredBoundary:
    boundary_id: str
    forbidden_module_prefixes: tuple[str, ...]
    forbidden_symbol_pairs: tuple[tuple[str, str], ...]
    owner_spec: str

    def __post_init__(self) -> None:
        _require_rule_id(self.boundary_id)
        if not self.forbidden_module_prefixes and not self.forbidden_symbol_pairs:
            raise ArchitectureConfigurationError(
                f"deferred boundary {self.boundary_id!r} has no matchers"
            )
        if self.forbidden_module_prefixes:
            _require_prefixes(
                self.forbidden_module_prefixes,
                field="forbidden_module_prefixes",
            )
        _require_symbol_pairs(self.forbidden_symbol_pairs)
        _require_reason(self.owner_spec)


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    rule_id: str
    source_module: str
    target: str
    line: int
    column: int
    mechanism: str
    reason: str


def _require_rule_id(value: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ArchitectureConfigurationError("rule id must be a trimmed non-empty string")


def _require_reason(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ArchitectureConfigurationError("rule reason/owner must be non-empty")


def _require_dotted_name(value: str, *, field: str) -> None:
    if type(value) is not str or _DOTTED_NAME.fullmatch(value) is None:
        raise ArchitectureConfigurationError(
            f"{field} must be an exact dotted Python name: {value!r}"
        )


def _require_prefixes(values: tuple[str, ...], *, field: str) -> None:
    if type(values) is not tuple or not values:
        raise ArchitectureConfigurationError(f"{field} must be a non-empty tuple")
    for value in values:
        _require_dotted_name(value, field=field)
    if len(set(values)) != len(values):
        raise ArchitectureConfigurationError(f"{field} contains duplicate prefixes")


def _require_optional_prefixes(values: tuple[str, ...], *, field: str) -> None:
    if type(values) is not tuple:
        raise ArchitectureConfigurationError(f"{field} must be a tuple")
    for value in values:
        _require_dotted_name(value, field=field)
    if len(set(values)) != len(values):
        raise ArchitectureConfigurationError(f"{field} contains duplicate prefixes")


def _require_identifiers(values: tuple[str, ...], *, field: str) -> None:
    if type(values) is not tuple:
        raise ArchitectureConfigurationError(f"{field} must be a tuple")
    for value in values:
        if type(value) is not str or not value.isidentifier():
            raise ArchitectureConfigurationError(
                f"{field} must contain exact Python identifiers: {value!r}"
            )
    if len(set(values)) != len(values):
        raise ArchitectureConfigurationError(f"{field} contains duplicates")


def _require_symbol_pairs(values: tuple[tuple[str, str], ...]) -> None:
    if type(values) is not tuple:
        raise ArchitectureConfigurationError("forbidden_symbol_pairs must be a tuple")
    for pair in values:
        if type(pair) is not tuple or len(pair) != 2:
            raise ArchitectureConfigurationError(
                "forbidden_symbol_pairs must contain (module, symbol) tuples"
            )
        module, symbol = pair
        _require_dotted_name(module, field="forbidden symbol owner")
        if type(symbol) is not str or not symbol.isidentifier():
            raise ArchitectureConfigurationError(
                f"forbidden symbol must be an exact Python identifier: {symbol!r}"
            )
    if len(set(values)) != len(values):
        raise ArchitectureConfigurationError("forbidden_symbol_pairs contains duplicates")


def _require_rule_tuple(
    values: tuple[object, ...],
    *,
    expected: type[object],
    field: str,
) -> None:
    if type(values) is not tuple:
        raise ArchitectureConfigurationError(f"{field} must be a tuple")
    for value in values:
        if type(value) is not expected:
            raise ArchitectureConfigurationError(
                f"{field} contains unsupported rule type: {type(value).__name__}"
            )


def module_matches_prefix(module: str, prefix: str) -> bool:
    """Match one module tree without substring/prefix confusion."""

    _require_dotted_name(module, field="module")
    _require_dotted_name(prefix, field="prefix")
    return module == prefix or module.startswith(f"{prefix}.")


def _matches_any(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module_matches_prefix(module, prefix) for prefix in prefixes)


def _resolve_relative(importer: str, level: int, module: str | None) -> str:
    package = importer.rpartition(".")[0]
    if not package:
        raise ArchitectureConfigurationError(
            f"relative import in top-level synthetic module {importer!r}"
        )
    relative = "." * level + (module or "")
    try:
        return importlib.util.resolve_name(relative, package)
    except (ImportError, ValueError) as exc:
        raise ArchitectureConfigurationError(
            f"cannot resolve {relative!r} from {importer!r}"
        ) from exc


def _import_base(importer: str, node: ast.ImportFrom) -> str:
    if node.level:
        return _resolve_relative(importer, node.level, node.module)
    if node.module is None:
        raise ArchitectureConfigurationError("absolute from-import has no module")
    return node.module


def collect_import_references(source: str, *, module_name: str) -> tuple[ImportReference, ...]:
    """Collect static imports and literal ``importlib.import_module`` calls.

    Comments, string contents, look-alike attributes and non-literal dynamic
    names are intentionally ignored.  This is an AST contract, not grep.
    """

    _require_dotted_name(module_name, field="module_name")
    if type(source) is not str:
        raise ArchitectureConfigurationError("source must be an exact string")
    tree = ast.parse(source, filename=f"<{module_name}>")
    importlib_aliases: set[str] = set()
    import_module_aliases: set[str] = set()
    references: list[ImportReference] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                references.append(
                    ImportReference(
                        target=alias.name,
                        symbol=None,
                        line=node.lineno,
                        column=node.col_offset,
                        mechanism=ImportMechanism.STATIC,
                    )
                )
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
                elif alias.name.startswith("importlib.") and alias.asname is None:
                    # ``import importlib.util`` binds the local name ``importlib``.
                    importlib_aliases.add("importlib")
        elif isinstance(node, ast.ImportFrom):
            base = _import_base(module_name, node)
            for alias in node.names:
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                references.append(
                    ImportReference(
                        target=target,
                        symbol=alias.name,
                        line=node.lineno,
                        column=node.col_offset,
                        mechanism=ImportMechanism.STATIC,
                    )
                )
                if base == "importlib" and alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)

    direct_import_module_aliases = frozenset(import_module_aliases)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id in direct_import_module_aliases
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    import_module_aliases.add(target.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant) or type(argument.value) is not str:
            continue
        is_import_module = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_aliases
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in import_module_aliases
        )
        if not is_import_module:
            continue
        target = argument.value
        if target.startswith("."):
            level = len(target) - len(target.lstrip("."))
            target = _resolve_relative(module_name, level, target[level:])
        else:
            _require_dotted_name(target, field="literal dynamic import target")
        references.append(
            ImportReference(
                target=target,
                symbol=None,
                line=node.lineno,
                column=node.col_offset,
                mechanism=ImportMechanism.LITERAL_IMPORTLIB,
            )
        )

    return tuple(
        sorted(
            set(references),
            key=lambda item: (
                item.line,
                item.column,
                item.mechanism.value,
                item.target,
                item.symbol or "",
            ),
        )
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


def _collect_import_aliases(tree: ast.AST, *, module_name: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.partition(".")[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom):
            base = _import_base(module_name, node)
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{base}.{alias.name}"
    return aliases


def _resolve_imported_name(raw_name: str, aliases: dict[str, str]) -> str | None:
    root, separator, remainder = raw_name.partition(".")
    imported = aliases.get(root)
    if imported is None:
        return None
    return imported if not separator else f"{imported}.{remainder}"


def collect_imported_calls(source: str, *, module_name: str) -> tuple[tuple[str, int, int], ...]:
    """Return qualified calls whose root was introduced by a real import."""

    _require_dotted_name(module_name, field="module_name")
    tree = ast.parse(source, filename=f"<{module_name}>")
    aliases = _collect_import_aliases(tree, module_name=module_name)

    calls: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw_name = _qualified_name(node.func)
        if raw_name is None:
            continue
        qualified = _resolve_imported_name(raw_name, aliases)
        if qualified is None:
            continue
        calls.add((qualified, node.lineno, node.col_offset))
    return tuple(sorted(calls, key=lambda item: (item[1], item[2], item[0])))


def collect_imported_attributes(
    source: str,
    *,
    module_name: str,
) -> tuple[tuple[str, int, int], ...]:
    """Resolve attribute use through real module aliases, not name text."""

    _require_dotted_name(module_name, field="module_name")
    tree = ast.parse(source, filename=f"<{module_name}>")
    aliases = _collect_import_aliases(tree, module_name=module_name)
    attributes: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        raw_name = _qualified_name(node)
        if raw_name is None:
            continue
        qualified = _resolve_imported_name(raw_name, aliases)
        if qualified is not None:
            attributes.add((qualified, node.lineno, node.col_offset))
    return tuple(sorted(attributes, key=lambda item: (item[1], item[2], item[0])))


@dataclass(frozen=True, slots=True)
class ArchitecturePolicy:
    parser_module_prefixes: tuple[str, ...]
    allowed_import_rules: tuple[AllowedImportRule, ...]
    import_rules: tuple[ForbiddenImportRule, ...]
    exclusive_call_rules: tuple[ExclusiveCallRule, ...]
    deferred_boundaries: tuple[DeferredBoundary, ...]

    def __post_init__(self) -> None:
        _require_prefixes(self.parser_module_prefixes, field="parser_module_prefixes")
        _require_rule_tuple(
            self.allowed_import_rules,
            expected=AllowedImportRule,
            field="allowed_import_rules",
        )
        _require_rule_tuple(
            self.import_rules,
            expected=ForbiddenImportRule,
            field="import_rules",
        )
        _require_rule_tuple(
            self.exclusive_call_rules,
            expected=ExclusiveCallRule,
            field="exclusive_call_rules",
        )
        _require_rule_tuple(
            self.deferred_boundaries,
            expected=DeferredBoundary,
            field="deferred_boundaries",
        )
        self._reject_ambiguous_allowlists()
        self._require_complete_parser_allowlist()
        self._reject_duplicate_rules()

    def _reject_ambiguous_allowlists(self) -> None:
        owners: list[tuple[str, str]] = []
        for rule in self.allowed_import_rules:
            for prefix in rule.importer_prefixes:
                for existing, existing_rule in owners:
                    if module_matches_prefix(prefix, existing) or module_matches_prefix(
                        existing,
                        prefix,
                    ):
                        raise ArchitectureConfigurationError(
                            "overlapping allowed-import owners: "
                            f"{existing_rule}/{existing} and {rule.rule_id}/{prefix}"
                        )
                owners.append((prefix, rule.rule_id))

    def _require_complete_parser_allowlist(self) -> None:
        expected = set(self.parser_module_prefixes)
        actual = {
            prefix
            for rule in self.allowed_import_rules
            for prefix in rule.importer_prefixes
        }
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if extra:
                details.append(f"extra: {', '.join(extra)}")
            raise ArchitectureConfigurationError(
                "parser allowed-import owner coverage mismatch ("
                + "; ".join(details)
                + ")"
            )

    def _reject_duplicate_rules(self) -> None:
        identifiers: set[str] = set()
        signatures: set[tuple[object, ...]] = set()
        all_rules: tuple[object, ...] = (
            *self.allowed_import_rules,
            *self.import_rules,
            *self.exclusive_call_rules,
            *self.deferred_boundaries,
        )
        for rule in all_rules:
            rule_id = getattr(rule, "rule_id", None) or getattr(rule, "boundary_id")
            if rule_id in identifiers:
                raise ArchitectureConfigurationError(f"duplicate rule id: {rule_id}")
            identifiers.add(rule_id)
            signature = _rule_signature(rule)
            if signature in signatures:
                raise ArchitectureConfigurationError(
                    f"duplicate rule definition: {rule_id}"
                )
            signatures.add(signature)

    def check_module(self, module: SourceModule) -> tuple[ArchitectureViolation, ...]:
        references = collect_import_references(module.source, module_name=module.name)
        violations: set[ArchitectureViolation] = set()
        for rule in self.allowed_import_rules:
            if not _matches_any(module.name, rule.importer_prefixes):
                continue
            for reference in references:
                root = reference.target.partition(".")[0]
                allowed = (
                    root in STDLIB_MODULE_ROOTS
                    or root in rule.allowed_external_roots
                    or _matches_any(reference.target, rule.allowed_prefixes)
                )
                if not allowed:
                    violations.add(
                        ArchitectureViolation(
                            rule_id=rule.rule_id,
                            source_module=module.name,
                            target=reference.target,
                            line=reference.line,
                            column=reference.column,
                            mechanism=reference.mechanism.value,
                            reason=rule.reason,
                        )
                    )
        for rule in self.import_rules:
            if not _matches_any(module.name, rule.importer_prefixes):
                continue
            for reference in references:
                if _matches_any(reference.target, rule.forbidden_prefixes):
                    violations.add(
                        ArchitectureViolation(
                            rule_id=rule.rule_id,
                            source_module=module.name,
                            target=reference.target,
                            line=reference.line,
                            column=reference.column,
                            mechanism=reference.mechanism.value,
                            reason=rule.reason,
                        )
                    )

        if _matches_any(module.name, self.parser_module_prefixes):
            imported_attributes = collect_imported_attributes(
                module.source,
                module_name=module.name,
            )
            for boundary in self.deferred_boundaries:
                for reference in references:
                    module_violation = (
                        bool(boundary.forbidden_module_prefixes)
                        and _matches_any(
                            reference.target,
                            boundary.forbidden_module_prefixes,
                        )
                    )
                    symbol_owner = reference.target.rpartition(".")[0]
                    symbol_violation = bool(symbol_owner) and any(
                        reference.symbol == symbol
                        and module_matches_prefix(symbol_owner, owner)
                        for owner, symbol in boundary.forbidden_symbol_pairs
                    )
                    if module_violation or symbol_violation:
                        violations.add(
                            ArchitectureViolation(
                                rule_id=f"deferred.{boundary.boundary_id}",
                                source_module=module.name,
                                target=reference.target,
                                line=reference.line,
                                column=reference.column,
                                mechanism=reference.mechanism.value,
                                reason=f"deferred to {boundary.owner_spec}",
                            )
                        )
                for attribute, line, column in imported_attributes:
                    attribute_owner, _, attribute_symbol = attribute.rpartition(".")
                    if any(
                        attribute_symbol == symbol
                        and module_matches_prefix(attribute_owner, owner)
                        for owner, symbol in boundary.forbidden_symbol_pairs
                    ):
                        violations.add(
                            ArchitectureViolation(
                                rule_id=f"deferred.{boundary.boundary_id}",
                                source_module=module.name,
                                target=attribute,
                                line=line,
                                column=column,
                                mechanism="imported_attribute",
                                reason=f"deferred to {boundary.owner_spec}",
                            )
                        )

        imported_calls = collect_imported_calls(module.source, module_name=module.name)
        for rule in self.exclusive_call_rules:
            if not _matches_any(module.name, rule.checked_importer_prefixes):
                continue
            if _matches_any(module.name, rule.allowed_importer_prefixes):
                continue
            grammar_modules = tuple(
                sorted({call.rpartition(".")[0] for call in rule.qualified_calls})
            )
            for reference in references:
                if reference.symbol == "*" and _matches_any(
                    reference.target,
                    grammar_modules,
                ):
                    violations.add(
                        ArchitectureViolation(
                            rule_id=rule.rule_id,
                            source_module=module.name,
                            target=f"{reference.target}.*",
                            line=reference.line,
                            column=reference.column,
                            mechanism="star_import",
                            reason=rule.reason,
                        )
                    )
            for call, line, column in imported_calls:
                if call in rule.qualified_calls:
                    violations.add(
                        ArchitectureViolation(
                            rule_id=rule.rule_id,
                            source_module=module.name,
                            target=call,
                            line=line,
                            column=column,
                            mechanism="exclusive_call",
                            reason=rule.reason,
                        )
                    )

        return tuple(
            sorted(
                violations,
                key=lambda item: (
                    item.source_module,
                    item.line,
                    item.column,
                    item.rule_id,
                    item.target,
                    item.mechanism,
                ),
            )
        )

    def check_modules(
        self,
        modules: Iterable[SourceModule],
    ) -> tuple[ArchitectureViolation, ...]:
        materialized = tuple(modules)
        names = tuple(module.name for module in materialized)
        duplicates = sorted(name for name in set(names) if names.count(name) > 1)
        if duplicates:
            raise ArchitectureConfigurationError(
                f"duplicate source modules: {', '.join(duplicates)}"
            )
        return tuple(
            sorted(
                (
                    violation
                    for module in materialized
                    for violation in self.check_module(module)
                ),
                key=lambda item: (
                    item.source_module,
                    item.line,
                    item.column,
                    item.rule_id,
                    item.target,
                ),
            )
        )


def _rule_signature(rule: object) -> tuple[object, ...]:
    if isinstance(rule, ForbiddenImportRule):
        return (
            "import",
            rule.importer_prefixes,
            rule.forbidden_prefixes,
        )
    if isinstance(rule, AllowedImportRule):
        return (
            "allow",
            rule.importer_prefixes,
            rule.allowed_prefixes,
            rule.allowed_external_roots,
        )
    if isinstance(rule, ExclusiveCallRule):
        return (
            "call",
            rule.qualified_calls,
            rule.checked_importer_prefixes,
            rule.allowed_importer_prefixes,
        )
    if isinstance(rule, DeferredBoundary):
        return (
            "deferred",
            rule.forbidden_module_prefixes,
            rule.forbidden_symbol_pairs,
        )
    raise ArchitectureConfigurationError(f"unsupported rule type: {type(rule).__name__}")


PARSER_MODULE_PREFIXES = (
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
)

PARSER_CODEC_PREFIXES = (
    "parser_localcat_codec",
    "parser_gettext_codec",
    "parser_tmx_codec",
    "parser_tm_json_codec",
    "parser_termbase_codec",
)

ENGINE_STORE_PREFIXES = (
    "tm_engine",
    "glossary_engine",
    "tm_sqlite_store",
    "tm_store",
    "termbase_store",
)

APPLICATION_FACADE_PREFIXES = (
    "editor_project",
    "resource_importer",
    "tm_json_importer",
    "logic_controller",
    "translation_runner",
    "stress_runner",
)

PLUGIN_IMPLEMENTATION_PREFIXES = (
    "rpy_project_codec",
    "rpy_codec_types",
    "rpy_tokens",
    "rpy_sidecar",
)

STDLIB_MODULE_ROOTS = tuple(sorted(sys.stdlib_module_names | {"__future__"}))

DEFERRED_BOUNDARY_MATRIX = (
    DeferredBoundary(
        "rpy_plugin_implementation",
        ("rpy_project_codec", "rpy_codec_types", "rpy_tokens", "rpy_sidecar"),
        (
            ("rpy_project_codec", "RpyProjectCodec"),
            ("rpy_tokens", "RpyToken"),
            ("rpy_sidecar", "RpySidecar"),
        ),
        "rpy-project-codec",
    ),
    DeferredBoundary(
        "workspace_authority",
        ("workspace", "workspace_state", "workspace_preferences"),
        (("workspace_state", "WorkspaceState"), ("workspace", "CurrentDocumentId")),
        "multi-document-project-workspace",
    ),
    DeferredBoundary(
        "multi_document_aggregation",
        ("multi_document", "multi_document_project_workspace", "project_document"),
        (
            ("project_document", "ProjectDocument"),
            ("multi_document", "DocumentId"),
            ("multi_document", "ProjectSegmentId"),
        ),
        "multi-document-project-workspace",
    ),
    DeferredBoundary(
        "project_package_reconciliation",
        ("project_package", "project_reconciliation"),
        (
            ("project_package", "ProjectPackage"),
            ("project_reconciliation", "ReconciliationReceipt"),
        ),
        "multi-document-project-workspace",
    ),
    DeferredBoundary(
        "collaborative_chunks",
        ("collaborative_job_chunks", "job_chunks", "chunk_authority"),
        (
            ("collaborative_job_chunks", "ChunkMembership"),
            ("collaborative_job_chunks", "ChunkPermission"),
        ),
        "collaborative-job-chunks",
    ),
    DeferredBoundary(
        "cross_device_sync",
        ("cross_device_sync", "sync_provider", "remote_provider"),
        (
            ("sync_provider", "SyncProvider"),
            ("cross_device_sync", "RemoteConflictPolicy"),
        ),
        "cross-device-sync-plugin",
    ),
    DeferredBoundary(
        "tm_storage_authority",
        ("tm_sqlite_store", "tm_store", "canonical_tm_store"),
        (
            ("tm_store", "TMStore"),
            ("canonical_tm_store", "CanonicalTMRecord"),
            ("tm_store", "TMActivationReceipt"),
        ),
        "tm-storage-retrieval-index",
    ),
    DeferredBoundary(
        "tmx_context_interchange",
        ("tmx_context", "tmx_export", "tmx_provenance"),
        (
            ("tmx_context", "TMXContext"),
            ("tmx_provenance", "TMXProvenance"),
        ),
        "tmx-context-interchange",
    ),
    DeferredBoundary(
        "speaker_profiles",
        ("speaker_profiles", "speaker_avatar"),
        (
            ("speaker_profiles", "SpeakerProfile"),
            ("speaker_avatar", "SpeakerAvatar"),
        ),
        "speaker-display-profiles",
    ),
    DeferredBoundary(
        "automatic_termbase_column_inference",
        ("termbase_column_inference",),
        (("termbase_column_inference", "LanguageColumnInference"),),
        "future termbase column-selection UI contract",
    ),
    DeferredBoundary(
        "other_deferred_format_codecs",
        ("xliff_codec", "office_codec", "pdf_codec", "ocr_codec"),
        (
            ("xliff_codec", "XliffCodec"),
            ("office_codec", "OfficeCodec"),
            ("pdf_codec", "PdfCodec"),
            ("ocr_codec", "OcrCodec"),
        ),
        "owning future format specs",
    ),
)


def build_parser_architecture_policy() -> ArchitecturePolicy:
    """Build the approved flat-repository Parser v1 dependency policy."""

    return ArchitecturePolicy(
        parser_module_prefixes=PARSER_MODULE_PREFIXES,
        allowed_import_rules=(
            AllowedImportRule(
                "contracts.allowed_dependencies",
                ("parser_contracts",),
                (),
                (),
                "neutral contracts are stdlib-only",
            ),
            AllowedImportRule(
                "source.allowed_dependencies",
                ("parser_source",),
                ("parser_contracts",),
                (),
                "source depends only on stdlib and contracts",
            ),
            AllowedImportRule(
                "registry.allowed_dependencies",
                ("parser_registry",),
                ("parser_contracts",),
                (),
                "registry depends only on stdlib and contracts",
            ),
            AllowedImportRule(
                "json_support.allowed_dependencies",
                ("parser_json_support",),
                ("parser_contracts",),
                (),
                "JSON support depends only on stdlib and contracts",
            ),
            AllowedImportRule(
                "xlsx_support.allowed_dependencies",
                ("parser_xlsx_support",),
                ("parser_contracts",),
                (),
                "XLSX preflight support depends only on stdlib and contracts",
            ),
            AllowedImportRule(
                "localcat_codec.allowed_dependencies",
                ("parser_localcat_codec",),
                ("parser_contracts", "parser_source", "parser_json_support"),
                (),
                "LocalCAT codec uses its Design-declared neutral dependencies",
            ),
            AllowedImportRule(
                "gettext_codec.allowed_dependencies",
                ("parser_gettext_codec",),
                ("parser_contracts", "parser_source"),
                (),
                "gettext codec uses its Design-declared neutral dependencies",
            ),
            AllowedImportRule(
                "tmx_codec.allowed_dependencies",
                ("parser_tmx_codec",),
                ("parser_contracts", "parser_source"),
                (),
                "TMX codec uses its Design-declared neutral dependencies",
            ),
            AllowedImportRule(
                "tm_json_codec.allowed_dependencies",
                ("parser_tm_json_codec",),
                ("parser_contracts", "parser_source", "parser_json_support"),
                (),
                "normalized TM JSON codec uses shared JSON support",
            ),
            AllowedImportRule(
                "termbase_codec.allowed_dependencies",
                ("parser_termbase_codec",),
                ("parser_contracts", "parser_source", "parser_xlsx_support"),
                ("openpyxl",),
                "termbase codec may conditionally use openpyxl after XLSX preflight",
            ),
            AllowedImportRule(
                "composition.allowed_dependencies",
                ("parser_composition",),
                (
                    "parser_contracts",
                    "parser_registry",
                    "parser_source",
                    *PARSER_CODEC_PREFIXES,
                ),
                (),
                "composition alone coordinates Source and built-in codecs for Application",
            ),
        ),
        import_rules=(
            ForbiddenImportRule(
                "engine_store.must_not_import_parser",
                ENGINE_STORE_PREFIXES,
                PARSER_MODULE_PREFIXES,
                "Engine and Store must not reverse-import Parser",
            ),
            ForbiddenImportRule(
                "application.parser_surface_only",
                APPLICATION_FACADE_PREFIXES,
                tuple(
                    module
                    for module in PARSER_MODULE_PREFIXES
                    if module not in {"parser_contracts", "parser_composition"}
                ),
                "Application imports only Parser contracts/composition",
            ),
            ForbiddenImportRule(
                "plugin.depends_on_neutral_contract_only",
                PLUGIN_IMPLEMENTATION_PREFIXES,
                tuple(
                    module
                    for module in PARSER_MODULE_PREFIXES
                    if module != "parser_contracts"
                ),
                "format plugins depend on the neutral port, not Foundation internals",
            ),
            ForbiddenImportRule(
                "plugin.must_not_import_localcat_authorities",
                PLUGIN_IMPLEMENTATION_PREFIXES,
                (*ENGINE_STORE_PREFIXES, *APPLICATION_FACADE_PREFIXES),
                "format plugins do not acquire LocalCAT Application/Engine/Store authority",
            ),
        ),
        exclusive_call_rules=(
            ExclusiveCallRule(
                "application.parser_surface_factory_only",
                (
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
                ),
                (*APPLICATION_FACADE_PREFIXES, "parser_composition"),
                ("parser_composition",),
                "Application uses the factory-created composition surface, not constructors",
            ),
            ExclusiveCallRule(
                "syntax.localcat_or_tm_json_owner",
                ("json.load", "json.loads"),
                (
                    "editor_project",
                    "tm_json_importer",
                    "parser_json_support",
                    "parser_localcat_codec",
                    "parser_tm_json_codec",
                ),
                (
                    "parser_json_support",
                    "parser_localcat_codec",
                    "parser_tm_json_codec",
                ),
                "JSON grammar calls are reserved for the shared support and owning codecs",
            ),
            ExclusiveCallRule(
                "syntax.termbase_csv_owner",
                ("csv.reader",),
                (
                    "resource_importer",
                    "glossary_engine",
                    "parser_termbase_codec",
                ),
                ("parser_termbase_codec",),
                "CSV row grammar has one codec owner",
            ),
            ExclusiveCallRule(
                "syntax.tmx_xml_owner",
                (
                    "xml.etree.ElementTree.fromstring",
                    "xml.etree.ElementTree.iterparse",
                    "xml.etree.ElementTree.parse",
                ),
                ("resource_importer", "parser_tmx_codec"),
                ("parser_tmx_codec",),
                "TMX grammar has one codec owner",
            ),
            ExclusiveCallRule(
                "syntax.termbase_xlsx_owner",
                ("openpyxl.load_workbook",),
                (
                    "resource_importer",
                    "glossary_engine",
                    "parser_termbase_codec",
                ),
                ("parser_termbase_codec",),
                "XLSX row selection has one codec owner",
            ),
        ),
        deferred_boundaries=DEFERRED_BOUNDARY_MATRIX,
    )
