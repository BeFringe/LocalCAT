"""Cluster 1 current-source architecture guards for the workspace boundary."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class _SourceModule:
    name: str
    source: str


@dataclass(frozen=True, slots=True)
class _ImportViolation:
    rule: str
    importer: str
    imported: str


# These are the only names reserved for workspace and downstream authorities.
# Cluster 1 authorizes an exact three-module subset below; every other spelling
# remains an explicit governance change, not a way around the guards.
_FUTURE_MODULE_PREFIXES_BY_OWNER = {
    "multi-document-project-workspace": (
        "multi_document",
        "multi_document_contracts",
        "multi_document_project_workspace",
        "multi_document_workspace",
        "project_document",
        "project_package",
        "project_package_manifest",
        "project_reconciliation",
        "project_save",
        "project_workspace",
    ),
    "collaborative-job-chunks": (
        "chunk_authority",
        "collaborative_job_chunks",
        "job_chunks",
    ),
    "cross-device-sync-plugin": (
        "cross_device_sync",
        "remote_provider",
        "sync_provider",
    ),
    "language-resource-portability": (
        "language_resource_portability",
        "resource_artifact_save",
        "resource_package",
        "resource_portability",
        "resource_receipt_ledger",
        "tm_resource_port",
    ),
}
_EXPECTED_FUTURE_MODULE_PREFIXES_BY_OWNER = {
    "multi-document-project-workspace": (
        "multi_document",
        "multi_document_contracts",
        "multi_document_project_workspace",
        "multi_document_workspace",
        "project_document",
        "project_package",
        "project_package_manifest",
        "project_reconciliation",
        "project_save",
        "project_workspace",
    ),
    "collaborative-job-chunks": (
        "chunk_authority",
        "collaborative_job_chunks",
        "job_chunks",
    ),
    "cross-device-sync-plugin": (
        "cross_device_sync",
        "remote_provider",
        "sync_provider",
    ),
    "language-resource-portability": (
        "language_resource_portability",
        "resource_artifact_save",
        "resource_package",
        "resource_portability",
        "resource_receipt_ledger",
        "tm_resource_port",
    ),
}
_WORKSPACE_MODULE_PREFIXES = (
    *_FUTURE_MODULE_PREFIXES_BY_OWNER["multi-document-project-workspace"],
)
_ALL_FUTURE_MODULE_PREFIXES = tuple(
    prefix
    for prefixes in _FUTURE_MODULE_PREFIXES_BY_OWNER.values()
    for prefix in prefixes
)

_WORKSPACE_AUTHORITY_NAMES = frozenset(
    {
        "DocumentId",
        "ProjectDocument",
        "ProjectDocumentWriterPort",
        "ProjectImportReceipt",
        "ProjectOrigin",
        "ProjectPackage",
        "ProjectPackageManifest",
        "ProjectRecoveryReport",
        "ProjectSaveReport",
        "ProjectSaveService",
        "ProjectSegment",
        "ProjectSegmentId",
        "ProjectWorkspacePersistencePort",
        "ProjectWorkspaceService",
        "ReconciliationPreview",
        "ReconciliationReceipt",
        "PendingRecoveryFacts",
        "WorkspaceSaveBaseline",
        "codec_private_member",
    }
)
_EXPECTED_WORKSPACE_AUTHORITY_NAMES = frozenset(
    {
        "DocumentId",
        "ProjectDocument",
        "ProjectDocumentWriterPort",
        "ProjectImportReceipt",
        "ProjectOrigin",
        "ProjectPackage",
        "ProjectPackageManifest",
        "ProjectRecoveryReport",
        "ProjectSaveReport",
        "ProjectSaveService",
        "ProjectSegment",
        "ProjectSegmentId",
        "ProjectWorkspacePersistencePort",
        "ProjectWorkspaceService",
        "ReconciliationPreview",
        "ReconciliationReceipt",
        "PendingRecoveryFacts",
        "WorkspaceSaveBaseline",
        "codec_private_member",
    }
)

_CONCRETE_CODEC_OR_ARCHIVE_PREFIXES = (
    "parser_gettext_codec",
    "parser_json_support",
    "parser_localcat_codec",
    "parser_termbase_codec",
    "parser_tm_json_codec",
    "parser_tmx_codec",
    "parser_xlsx_support",
    "tarfile",
    "zipfile",
)
_APPLICATION_PARSER_FACADES = frozenset(
    {"parser_composition", "parser_contracts"}
)
_APPLICATION_WORKSPACE_IMPORT_ALLOWLIST = {
    "editor_contracts": frozenset(
        {
            "project_workspace_identity.validate_document_id",
            "project_workspace_identity.validate_local_segment_id",
            "project_workspace_identity.validate_project_id",
        }
    ),
    "editor_controller": frozenset(
        {
            "project_package.OpenedProjectPackage",
            "project_package.ProjectPackageExportReceipt",
            "project_package.ProjectPackageImportPreview",
            "project_package.ProjectPackageImportMode",
            "project_package.ProjectPackageImportReceipt",
            "project_package.ProjectPackagePersistenceBinding",
            "project_package.ProjectPackageService",
            "project_save.ProjectSaveService",
            "project_save.ProjectSaveReport",
            "project_workspace.DocumentProgress",
            "project_workspace.FlatProjectSegment",
            "project_workspace.IssuedDocumentIdentity",
            "project_workspace.IssuedProjectIdentity",
            "project_workspace.IssuedSegmentIdentity",
            "project_workspace.ProjectProgress",
            "project_workspace.ProjectWorkspaceService",
            "project_workspace.ReconciliationAssociation",
            "project_workspace.ReconciliationDecision",
            "project_workspace.ReconciliationPreview",
            "project_workspace.ReconciliationReceipt",
            "project_workspace.WorkspaceDocumentView",
            "project_workspace.WorkspaceSaveState",
            "project_workspace.WorkspaceSegmentView",
            "project_workspace.WorkspaceSessionView",
            "project_workspace_contracts.ProjectSegment",
            "project_workspace_contracts.ProjectWorkspace",
            "project_workspace_contracts.SegmentIdentity",
            "project_workspace_contracts.StagedSelectedProjectDocuments",
            "project_workspace_identity.ProjectWorkspaceError",
            "project_workspace_intake.OriginRenameMapping",
            "project_workspace_intake.SelectedProjectDocumentsRequest",
            "project_workspace_intake.revalidate_staged_selected_documents",
            "project_workspace_intake.stage_selected_project_documents",
            "project_workspace_intake.stage_workspace_rebind",
            "resource_package_contracts.ResourceExportOutcome",
            "resource_package_contracts.PortableResourceKind",
            "resource_package_contracts.ResourceImportMode",
            "resource_package_contracts.ResourcePackageImportPreview",
            "resource_package_contracts.ResourcePackageImportResult",
            "resource_package_contracts.ResourcePackageValidationReport",
            "resource_package_contracts.ResourcePortabilityError",
            "resource_package_contracts.ResourceRecoveryAction",
            "resource_package_contracts.ResourceRecoveryDisposition",
            "resource_package_contracts.ResourceRecoveryOutcome",
            "resource_package_contracts.ResourceRecoveryPreview",
            "resource_portability.ResourcePortabilityService",
        }
    ),
    "project_search": frozenset(
        {
            "project_workspace.ProjectWorkspaceService",
            "project_workspace_contracts.ProjectSegment",
        }
    ),
}
_CURRENT_WORKSPACE_PRODUCTION_MODULES = frozenset(
    {
        "editor_project_workspace_adapter",
        "project_package",
        "project_save",
        "project_workspace",
        "project_workspace_contracts",
        "project_workspace_identity",
        "project_workspace_intake",
        "resource_artifact_save",
        "resource_package",
        "resource_package_contracts",
        "resource_portability",
        "resource_receipt_ledger",
        "tm_resource_port",
    }
)
_WORKSPACE_ADAPTER_MODULE = "editor_project_workspace_adapter"
_WORKSPACE_ADAPTER_LOCAL_IMPORT_ALLOWLIST = frozenset(
    {
        "editor_contracts",
        "parser_composition",
        "parser_contracts",
        "project_workspace_contracts",
        "project_workspace_identity",
    }
)
_WORKSPACE_CORE_MODULE = "project_workspace"
_WORKSPACE_CORE_LOCAL_IMPORT_ALLOWLIST = frozenset(
    {
        "project_workspace_contracts",
        "project_workspace_identity",
    }
)
_WORKSPACE_INTAKE_MODULE = "project_workspace_intake"
_WORKSPACE_INTAKE_LOCAL_IMPORT_ALLOWLIST = frozenset(
    {
        "parser_composition",
        "parser_contracts",
        "project_workspace_contracts",
        "project_workspace_identity",
    }
)
_WORKSPACE_SAVE_MODULE = "project_save"
_WORKSPACE_SAVE_LOCAL_IMPORT_ALLOWLIST = frozenset(
    {
        "project_workspace",
        "project_workspace_contracts",
        "project_workspace_identity",
    }
)
_WORKSPACE_PACKAGE_MODULE = "project_package"
_WORKSPACE_PACKAGE_LOCAL_IMPORT_ALLOWLIST = frozenset(
    {
        "parser_contracts",
        "project_save",
        "project_workspace",
        "project_workspace_contracts",
        "project_workspace_identity",
    }
)
_WORKSPACE_FORBIDDEN_FUTURE_PREFIXES = (
    *_FUTURE_MODULE_PREFIXES_BY_OWNER["collaborative-job-chunks"],
    *_FUTURE_MODULE_PREFIXES_BY_OWNER["cross-device-sync-plugin"],
    *_FUTURE_MODULE_PREFIXES_BY_OWNER["language-resource-portability"],
)
_WORKSPACE_QT_RUNTIME_PREFIXES = (
    "PyQt5",
    "PyQt6",
    "PySide6",
    "shiboken6",
)
_COMPUTED_IMPORT_TARGET = "<computed-import-target>"


def _production_paths(root: Path = _ROOT) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.name.startswith("test_"):
            continue
        paths.append(path)
    return tuple(sorted(paths))


def _module_name(path: Path, *, root: Path = _ROOT) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)


def _production_modules(root: Path = _ROOT) -> dict[str, _SourceModule]:
    return {
        _module_name(path, root=root): _SourceModule(
            _module_name(path, root=root),
            path.read_text(encoding="utf-8"),
        )
        for path in _production_paths(root)
    }


def _matches_prefix(module_name: str, prefix: str) -> bool:
    return module_name == prefix or module_name.startswith(f"{prefix}.")


def _matches_any_prefix(module_name: str, prefixes: tuple[str, ...]) -> bool:
    return any(_matches_prefix(module_name, prefix) for prefix in prefixes)


def _matches_reserved_prefix(
    module_name: str,
    prefixes: tuple[str, ...],
) -> bool:
    segments = module_name.split(".")
    return any(
        segment == prefix or segment.startswith(f"{prefix}_")
        for prefix in prefixes
        for segment in segments
    )


def _matches_future_prefix(module_name: str) -> bool:
    """Fail closed on registered future names and suffixed sibling modules."""

    return _matches_reserved_prefix(module_name, _ALL_FUTURE_MODULE_PREFIXES)


def _import_from_base(module_name: str, node: ast.ImportFrom) -> str:
    if not node.level:
        if node.module is None:
            raise AssertionError("absolute from-import has no module")
        return node.module
    package = module_name.rpartition(".")[0]
    if not package:
        raise AssertionError("top-level production modules cannot use relative imports")
    return importlib.util.resolve_name(
        "." * node.level + (node.module or ""),
        package,
    )


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> tuple[tuple[str, ast.AST], ...]:
    value = node.value
    if value is None:
        return ()
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(
        (target.id, value)
        for target in targets
        if isinstance(target, ast.Name)
    )


def _is_loader_expression(
    node: ast.AST,
    *,
    importlib_aliases: set[str],
    builtins_aliases: set[str],
    loader_aliases: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in loader_aliases
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
    ):
        return (
            node.attr == "import_module"
            and node.value.id in importlib_aliases
        ) or (
            node.attr == "__import__"
            and node.value.id in builtins_aliases
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and isinstance(node.args[1], ast.Constant)
        and type(node.args[1].value) is str
    ):
        owner = node.args[0].id
        attribute = node.args[1].value
        return (
            attribute == "import_module" and owner in importlib_aliases
        ) or (
            attribute == "__import__" and owner in builtins_aliases
        )
    return False


def _dynamic_import_argument(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    return next(
        (
            keyword.value
            for keyword in node.keywords
            if keyword.arg in {"name", "module"}
        ),
        None,
    )


def _collect_import_targets(module: _SourceModule) -> frozenset[str]:
    """Collect static and literal dynamic imports without reading text tokens."""

    tree = ast.parse(module.source, filename=f"<{module.name}>")
    targets: set[str] = set()
    importlib_aliases: set[str] = set()
    builtins_aliases: set[str] = set()
    loader_aliases: set[str] = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
                elif alias.name == "builtins":
                    builtins_aliases.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(module.name, node)
            for alias in node.names:
                targets.add(base if alias.name == "*" else f"{base}.{alias.name}")
                if base == "importlib" and alias.name == "import_module":
                    loader_aliases.add(alias.asname or alias.name)
                elif base == "builtins" and alias.name == "__import__":
                    loader_aliases.add(alias.asname or alias.name)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            for name, value in _assignment_names(node):
                if isinstance(value, ast.Name) and value.id in importlib_aliases:
                    if name not in importlib_aliases:
                        importlib_aliases.add(name)
                        changed = True
                elif isinstance(value, ast.Name) and value.id in builtins_aliases:
                    if name not in builtins_aliases:
                        builtins_aliases.add(name)
                        changed = True
                elif _is_loader_expression(
                    value,
                    importlib_aliases=importlib_aliases,
                    builtins_aliases=builtins_aliases,
                    loader_aliases=loader_aliases,
                ):
                    if name not in loader_aliases:
                        loader_aliases.add(name)
                        changed = True

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_loader_expression(
            node.func,
            importlib_aliases=importlib_aliases,
            builtins_aliases=builtins_aliases,
            loader_aliases=loader_aliases,
        ):
            continue
        argument = _dynamic_import_argument(node)
        if not isinstance(argument, ast.Constant) or type(argument.value) is not str:
            targets.add(_COMPUTED_IMPORT_TARGET)
            continue
        target = argument.value
        if target.startswith("."):
            package = module.name.rpartition(".")[0]
            if not package:
                raise AssertionError("top-level production modules cannot import relatively")
            target = importlib.util.resolve_name(target, package)
        targets.add(target)

    return frozenset(targets)


def _defined_authority_names(module: _SourceModule) -> frozenset[str]:
    tree = ast.parse(module.source, filename=f"<{module.name}>")
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names & _WORKSPACE_AUTHORITY_NAMES)


def _is_parser_or_codec(module_name: str) -> bool:
    leaf = module_name.rpartition(".")[2]
    return (
        leaf.startswith("parser_")
        or leaf.endswith("_codec")
        or "_codec_" in leaf
    )


def _is_editor_or_application(module_name: str) -> bool:
    leaf = module_name.rpartition(".")[2]
    return (
        leaf.startswith("editor_")
        or leaf.endswith("_controller")
        or leaf.endswith("_importer")
        or leaf.endswith("_runner")
        or leaf.endswith("_application")
        or "_application_" in leaf
    ) and not _is_parser_or_codec(module_name)


def _editor_or_application_module_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if (
                segment.startswith("editor_")
                or segment.endswith("_controller")
                or segment.endswith("_importer")
                or segment.endswith("_runner")
                or segment.endswith("_application")
                or "_application_" in segment
            )
            and not _is_parser_or_codec(segment)
        ),
        None,
    )


def _is_qt(module_name: str) -> bool:
    leaf = module_name.rpartition(".")[2]
    return leaf == "qt" or leaf.startswith("qt_")


def _is_tm_store_or_engine(module_name: str) -> bool:
    leaf = module_name.rpartition(".")[2]
    return leaf.startswith("tm_") or leaf.endswith("_store") or leaf.endswith("_engine")


def _is_future_workspace(module_name: str) -> bool:
    return _matches_reserved_prefix(module_name, _WORKSPACE_MODULE_PREFIXES)


def _parser_module_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if segment == "parser" or segment.startswith("parser_")
        ),
        None,
    )


def _qt_module_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if segment == "qt" or segment.startswith("qt_")
        ),
        None,
    )


def _tm_store_or_engine_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if segment.startswith("tm_")
            or segment.endswith("_store")
            or segment.endswith("_engine")
        ),
        None,
    )


def _provider_module_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if segment == "provider"
            or segment.startswith("provider_")
            or segment.endswith("_provider")
        ),
        None,
    )


def _chunk_module_segment(module_name: str) -> str | None:
    return next(
        (
            segment
            for segment in module_name.split(".")
            if segment in {"chunk", "chunks"}
            or segment.startswith("chunk_")
            or segment.endswith("_chunk")
            or segment.endswith("_chunks")
        ),
        None,
    )


def _workspace_adapter_import_is_forbidden(target: str) -> bool:
    if target == _COMPUTED_IMPORT_TARGET:
        return True
    root = target.partition(".")[0]
    if root in sys.stdlib_module_names or root == "__future__":
        return False
    return root not in _WORKSPACE_ADAPTER_LOCAL_IMPORT_ALLOWLIST


def _workspace_local_import_is_forbidden(
    target: str,
    *,
    allowlist: frozenset[str],
) -> bool:
    if target == _COMPUTED_IMPORT_TARGET:
        return True
    root = target.partition(".")[0]
    if root in sys.stdlib_module_names or root == "__future__":
        return False
    return root not in allowlist


def _boundary_violations(
    modules: dict[str, _SourceModule],
) -> tuple[_ImportViolation, ...]:
    violations: set[_ImportViolation] = set()
    for name, module in modules.items():
        targets = _collect_import_targets(module)
        rules: list[tuple[str, tuple[str, ...]]] = []
        if _is_parser_or_codec(name):
            rules.append(("parser-no-future-workspace", _ALL_FUTURE_MODULE_PREFIXES))
        if (
            _is_editor_or_application(name)
            or name in _APPLICATION_WORKSPACE_IMPORT_ALLOWLIST
        ) and name != _WORKSPACE_ADAPTER_MODULE:
            rules.append(
                (
                    "application-no-codec-or-future-authority",
                    (
                        *_CONCRETE_CODEC_OR_ARCHIVE_PREFIXES,
                        *_ALL_FUTURE_MODULE_PREFIXES,
                    ),
                )
            )
        if (
            _is_future_workspace(name) or name == _WORKSPACE_SAVE_MODULE
        ) and name != _WORKSPACE_PACKAGE_MODULE:
            rules.append(
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    (
                        *_CONCRETE_CODEC_OR_ARCHIVE_PREFIXES,
                        *_WORKSPACE_FORBIDDEN_FUTURE_PREFIXES,
                        *_WORKSPACE_QT_RUNTIME_PREFIXES,
                    ),
                )
            )
        if _is_qt(name):
            rules.append(
                (
                    "qt-no-parser-manifest-or-package",
                    (
                        "parser",
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
                        "project_manifest",
                        "package_manifest",
                        *_ALL_FUTURE_MODULE_PREFIXES,
                    ),
                )
            )
        if _is_tm_store_or_engine(name):
            rules.append(("tm-no-future-workspace", _WORKSPACE_MODULE_PREFIXES))
        if name == _WORKSPACE_ADAPTER_MODULE:
            for target in targets:
                if _workspace_adapter_import_is_forbidden(target):
                    violations.add(
                        _ImportViolation(
                            "workspace-adapter-exact-local-imports",
                            name,
                            target,
                        )
                    )
        if name == _WORKSPACE_CORE_MODULE:
            for target in targets:
                if _workspace_local_import_is_forbidden(
                    target,
                    allowlist=_WORKSPACE_CORE_LOCAL_IMPORT_ALLOWLIST,
                ):
                    violations.add(
                        _ImportViolation(
                            "workspace-core-exact-local-imports",
                            name,
                            target,
                        )
                    )
        if name == _WORKSPACE_INTAKE_MODULE:
            for target in targets:
                if _workspace_local_import_is_forbidden(
                    target,
                    allowlist=_WORKSPACE_INTAKE_LOCAL_IMPORT_ALLOWLIST,
                ):
                    violations.add(
                        _ImportViolation(
                            "workspace-intake-exact-local-imports",
                            name,
                            target,
                        )
                    )
        if name == _WORKSPACE_SAVE_MODULE:
            for target in targets:
                if _workspace_local_import_is_forbidden(
                    target,
                    allowlist=_WORKSPACE_SAVE_LOCAL_IMPORT_ALLOWLIST,
                ):
                    violations.add(
                        _ImportViolation(
                            "workspace-save-exact-local-imports",
                            name,
                            target,
                        )
                    )
        if name == _WORKSPACE_PACKAGE_MODULE:
            for target in targets:
                if _workspace_local_import_is_forbidden(
                    target,
                    allowlist=_WORKSPACE_PACKAGE_LOCAL_IMPORT_ALLOWLIST,
                ):
                    violations.add(
                        _ImportViolation(
                            "workspace-package-exact-local-imports",
                            name,
                            target,
                        )
                    )
        for rule, forbidden_prefixes in rules:
            for target in targets:
                parser_segment = _parser_module_segment(target)
                matches_rule_prefix = (
                    target == _COMPUTED_IMPORT_TARGET
                    or _matches_any_prefix(target, forbidden_prefixes)
                    or _matches_reserved_prefix(target, forbidden_prefixes)
                )
                if rule in {
                    "parser-no-future-workspace",
                    "application-no-codec-or-future-authority",
                    "qt-no-parser-manifest-or-package",
                } and _matches_future_prefix(target):
                    matches_rule_prefix = True
                if rule == "tm-no-future-workspace" and _matches_reserved_prefix(
                    target,
                    _WORKSPACE_MODULE_PREFIXES,
                ):
                    matches_rule_prefix = True
                if (
                    rule in {
                        "application-no-codec-or-future-authority",
                        "workspace-no-concrete-cross-layer-dependency",
                    }
                    and parser_segment is not None
                    and parser_segment not in _APPLICATION_PARSER_FACADES
                ):
                    matches_rule_prefix = True
                if (
                    rule == "workspace-no-concrete-cross-layer-dependency"
                    and (
                        _qt_module_segment(target) is not None
                        or _tm_store_or_engine_segment(target) is not None
                        or _provider_module_segment(target) is not None
                        or _chunk_module_segment(target) is not None
                        or (
                            parser_segment is None
                            and _editor_or_application_module_segment(target)
                            is not None
                        )
                    )
                ):
                    matches_rule_prefix = True
                if (
                    rule == "qt-no-parser-manifest-or-package"
                    and parser_segment is not None
                ):
                    matches_rule_prefix = True
                if (
                    rule == "application-no-codec-or-future-authority"
                    and matches_rule_prefix
                    and target
                    in _APPLICATION_WORKSPACE_IMPORT_ALLOWLIST.get(
                        name,
                        frozenset(),
                    )
                ):
                    matches_rule_prefix = False
                if matches_rule_prefix:
                    violations.add(_ImportViolation(rule, name, target))
    return tuple(sorted(violations, key=lambda item: (item.rule, item.importer, item.imported)))


class MultiDocumentCluster1GuardSelfTests(unittest.TestCase):
    def test_import_guard_uses_ast_and_catches_static_and_literal_dynamic_mutations(
        self,
    ) -> None:
        harmless = _SourceModule(
            "editor_future_panel",
            '"""import parser_localcat_codec"""\n'
            "# import project_package\n"
            "label = 'resource_package'\n",
        )
        static = _SourceModule("editor_future_panel", "import parser_localcat_codec\n")
        dynamic = _SourceModule(
            "editor_future_panel",
            "import importlib as il\n"
            "loader = il.import_module\n"
            "again = loader\n"
            "again('project_package')\n",
        )

        self.assertEqual(_boundary_violations({harmless.name: harmless}), ())
        self.assertEqual(
            {item.imported for item in _boundary_violations({static.name: static})},
            {"parser_localcat_codec"},
        )
        self.assertEqual(
            {item.imported for item in _boundary_violations({dynamic.name: dynamic})},
            {"project_package"},
        )

    def test_importlib_module_alias_chain_cannot_hide_import_module(self) -> None:
        mutation = _SourceModule(
            "editor_future_panel",
            "import importlib as imported\n"
            "module_alias = imported\n"
            "second_alias = module_alias\n"
            "second_alias.import_module('project_package')\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"project_package"},
        )

    def test_literal_getattr_loader_cannot_hide_import_module(self) -> None:
        mutation = _SourceModule(
            "editor_future_panel",
            "import importlib as imported\n"
            "module_alias = imported\n"
            "loader = getattr(module_alias, 'import_module')\n"
            "loader('project_package')\n"
            "getattr(module_alias, 'import_module')('resource_package')\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"project_package", "resource_package"},
        )

    def test_computed_dynamic_import_target_fails_closed(self) -> None:
        mutation = _SourceModule(
            "editor_future_panel",
            "import importlib\n"
            "target = 'project_' + 'package'\n"
            "importlib.import_module(target)\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {_COMPUTED_IMPORT_TARGET},
        )

    def test_future_workspace_cannot_import_concrete_parser_codec(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "import parser_localcat_codec\n"
            "from parser_source import SealedSourceSnapshot\n",
        )

        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({mutation.name: mutation})},
            {
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "parser_localcat_codec",
                ),
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "parser_source.SealedSourceSnapshot",
                ),
                (
                    "workspace-core-exact-local-imports",
                    "parser_localcat_codec",
                ),
                (
                    "workspace-core-exact-local-imports",
                    "parser_source.SealedSourceSnapshot",
                ),
            },
        )

    def test_future_workspace_cannot_import_qt_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "import localcat.qt_editor\n"
            "from PySide6.QtCore import QObject\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"localcat.qt_editor", "PySide6.QtCore.QObject"},
        )

    def test_future_workspace_cannot_import_tm_store_or_engine_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "from tm_engine import TMEngine\n"
            "import localcat.termbase_store\n"
            "import glossary_engine\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {
                "tm_engine.TMEngine",
                "localcat.termbase_store",
                "glossary_engine",
            },
        )

    def test_future_workspace_cannot_import_cross_device_provider_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "from localcat.sync_provider import SyncProvider\n"
            "import providers.s3_provider\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"localcat.sync_provider.SyncProvider", "providers.s3_provider"},
        )

    def test_future_workspace_cannot_import_resource_package_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "from resource_package import ResourcePackage\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"resource_package.ResourcePackage"},
        )

    def test_future_workspace_cannot_import_collaboration_chunk_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace",
            "from collaborative_job_chunks import ChunkMembership\n"
            "import localcat.chunk_assignment\n",
        )

        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {
                "collaborative_job_chunks.ChunkMembership",
                "localcat.chunk_assignment",
            },
        )

    def test_workspace_contract_leaf_cannot_import_editor_family(self) -> None:
        mutation = _SourceModule(
            "project_workspace_contracts",
            "from editor_controller import EditorController\n",
        )

        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({mutation.name: mutation})},
            {
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "editor_controller.EditorController",
                )
            },
        )

    def test_workspace_adapter_accepts_only_approved_local_seams(self) -> None:
        approved = _SourceModule(
            _WORKSPACE_ADAPTER_MODULE,
            "import os\n"
            "from pathlib import Path\n"
            "from editor_contracts import EditorProject\n"
            "from parser_composition import create_parser_application_surface\n"
            "from parser_contracts import ParsedSegment\n"
            "from project_workspace_contracts import ProjectWorkspace\n"
            "from project_workspace_identity import validate_project_id\n",
        )
        forbidden = _SourceModule(
            _WORKSPACE_ADAPTER_MODULE,
            "from editor_project import load_project\n"
            "from parser_source import SealedSourceSnapshot\n",
        )

        self.assertEqual(_boundary_violations({approved.name: approved}), ())
        self.assertEqual(
            {item.imported for item in _boundary_violations({forbidden.name: forbidden})},
            {"editor_project.load_project", "parser_source.SealedSourceSnapshot"},
        )

    def test_workspace_adapter_dynamic_import_alias_fails_closed(self) -> None:
        mutation = _SourceModule(
            _WORKSPACE_ADAPTER_MODULE,
            "import importlib as imported\n"
            "module_alias = imported\n"
            "module_alias.import_module('tm_engine')\n",
        )

        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({mutation.name: mutation})},
            {("workspace-adapter-exact-local-imports", "tm_engine")},
        )

    def test_workspace_core_has_no_parser_or_intake_dependency(self) -> None:
        approved = _SourceModule(
            _WORKSPACE_CORE_MODULE,
            "import hashlib\n"
            "from project_workspace_contracts import ProjectWorkspace\n"
            "from project_workspace_identity import validate_project_id\n",
        )
        forbidden = _SourceModule(
            _WORKSPACE_CORE_MODULE,
            "from parser_composition import create_parser_application_surface\n"
            "from project_workspace_intake import stage_selected_project_documents\n",
        )

        self.assertEqual(_boundary_violations({approved.name: approved}), ())
        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({forbidden.name: forbidden})},
            {
                (
                    "workspace-core-exact-local-imports",
                    "parser_composition.create_parser_application_surface",
                ),
                (
                    "workspace-core-exact-local-imports",
                    "project_workspace_intake.stage_selected_project_documents",
                ),
            },
        )

    def test_workspace_intake_accepts_only_neutral_parser_facades(self) -> None:
        approved = _SourceModule(
            _WORKSPACE_INTAKE_MODULE,
            "import os\n"
            "from parser_composition import create_parser_application_surface\n"
            "from parser_contracts import ParsedSegment\n"
            "from project_workspace_contracts import ProjectWorkspace\n"
            "from project_workspace_identity import validate_project_id\n",
        )
        forbidden = _SourceModule(
            _WORKSPACE_INTAKE_MODULE,
            "from parser_source import create_sealed_snapshot\n"
            "from parser_localcat_codec import LocalcatProjectCodec\n"
            "from project_workspace import ProjectWorkspaceService\n",
        )

        self.assertEqual(_boundary_violations({approved.name: approved}), ())
        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({forbidden.name: forbidden})},
            {
                (
                    "workspace-intake-exact-local-imports",
                    "parser_source.create_sealed_snapshot",
                ),
                (
                    "workspace-intake-exact-local-imports",
                    "parser_localcat_codec.LocalcatProjectCodec",
                ),
                (
                    "workspace-intake-exact-local-imports",
                    "project_workspace.ProjectWorkspaceService",
                ),
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "parser_source.create_sealed_snapshot",
                ),
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "parser_localcat_codec.LocalcatProjectCodec",
                ),
            },
        )

    def test_workspace_save_is_carrier_neutral_and_depends_only_on_workspace(self) -> None:
        approved = _SourceModule(
            _WORKSPACE_SAVE_MODULE,
            "from project_workspace import ProjectWorkspaceService\n"
            "from project_workspace_contracts import ProjectWorkspace\n"
            "from project_workspace_identity import validate_project_id\n",
        )
        forbidden = _SourceModule(
            _WORKSPACE_SAVE_MODULE,
            "import zipfile\n"
            "from parser_composition import create_parser_application_surface\n"
            "from qt_editor import EditorWindow\n"
            "from sync_provider import SyncProvider\n",
        )

        self.assertEqual(_boundary_violations({approved.name: approved}), ())
        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({forbidden.name: forbidden})},
            {
                ("workspace-no-concrete-cross-layer-dependency", "zipfile"),
                (
                    "workspace-save-exact-local-imports",
                    "parser_composition.create_parser_application_surface",
                ),
                (
                    "workspace-save-exact-local-imports",
                    "qt_editor.EditorWindow",
                ),
                ("workspace-save-exact-local-imports", "sync_provider.SyncProvider"),
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "qt_editor.EditorWindow",
                ),
                (
                    "workspace-no-concrete-cross-layer-dependency",
                    "sync_provider.SyncProvider",
                ),
            },
        )

    def test_save_authority_cannot_be_reverse_imported_across_layers(self) -> None:
        mutations = (
            _SourceModule(
                "parser_future_codec",
                "from project_save import ProjectSaveService\n",
            ),
            _SourceModule(
                "qt_future_window",
                "from project_save import ProjectSaveService\n",
            ),
            _SourceModule(
                "tm_future_engine",
                "from project_save import ProjectSaveService\n",
            ),
            _SourceModule(
                "editor_future_controller",
                "from project_save import ProjectSaveService\n",
            ),
        )

        for mutation in mutations:
            with self.subTest(importer=mutation.name):
                self.assertNotEqual(
                    _boundary_violations({mutation.name: mutation}),
                    (),
                )

    def test_other_editor_static_alias_cannot_reverse_import_workspace(self) -> None:
        mutation = _SourceModule(
            "editor_other_adapter",
            "import project_workspace_contracts as workspace_contracts\n",
        )

        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({mutation.name: mutation})},
            {
                (
                    "application-no-codec-or-future-authority",
                    "project_workspace_contracts",
                )
            },
        )

    def test_other_application_dynamic_alias_cannot_reverse_import_workspace(self) -> None:
        mutation = _SourceModule(
            "future_controller",
            "import importlib as imported\n"
            "module_alias = imported\n"
            "module_alias.import_module('project_workspace_identity')\n",
        )

        self.assertEqual(
            {(item.rule, item.imported) for item in _boundary_violations({mutation.name: mutation})},
            {
                (
                    "application-no-codec-or-future-authority",
                    "project_workspace_identity",
                )
            },
        )

    def test_nested_future_authority_spelling_is_reserved(self) -> None:
        mutation = _SourceModule(
            "editor_future_panel",
            "import localcat.project_package\n",
        )

        self.assertTrue(_matches_future_prefix("localcat.project_package"))
        self.assertEqual(
            {item.imported for item in _boundary_violations({mutation.name: mutation})},
            {"localcat.project_package"},
        )

    def test_production_discovery_is_recursive_and_excludes_test_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "root_module.py").write_text("value = 1\n", encoding="utf-8")
            package = root / "localcat" / "nested"
            package.mkdir(parents=True)
            (package / "production.py").write_text("value = 2\n", encoding="utf-8")
            tests = root / "localcat" / "tests"
            tests.mkdir()
            (tests / "helper.py").write_text("value = 3\n", encoding="utf-8")

            self.assertEqual(
                set(_production_modules(root)),
                {"localcat.nested.production", "root_module"},
            )

    def test_family_selection_is_semantic_not_a_fixed_file_allowlist(self) -> None:
        synthetic = {
            "new_project_controller": _SourceModule(
                "new_project_controller",
                "import zipfile\n",
            ),
            "qt_new_workspace_panel": _SourceModule(
                "qt_new_workspace_panel",
                "from parser_contracts import ParsedDocument\n",
            ),
            "rpy_project_codec": _SourceModule(
                "rpy_project_codec",
                "import multi_document_workspace\n",
            ),
            "tm_future_helper": _SourceModule(
                "tm_future_helper",
                "from project_document import ProjectDocument\n",
            ),
            "qt_new_codec_panel": _SourceModule(
                "qt_new_codec_panel",
                "import parser_future_codec\n",
            ),
        }

        self.assertEqual(
            {(item.rule, item.importer) for item in _boundary_violations(synthetic)},
            {
                ("application-no-codec-or-future-authority", "new_project_controller"),
                ("qt-no-parser-manifest-or-package", "qt_new_workspace_panel"),
                ("parser-no-future-workspace", "rpy_project_codec"),
                ("tm-no-future-workspace", "tm_future_helper"),
                ("qt-no-parser-manifest-or-package", "qt_new_codec_panel"),
            },
        )

    def test_authority_definition_guard_ignores_text_but_rejects_real_definitions(
        self,
    ) -> None:
        harmless = _SourceModule(
            "parser_future_codec",
            '"class ProjectDocument: pass"\n# codec_private_member = object()\n',
        )
        classes_and_field = _SourceModule(
            "parser_future_codec",
            "class ProjectDocument:\n"
            "    codec_private_member: bytes\n",
        )

        self.assertEqual(_defined_authority_names(harmless), frozenset())
        self.assertEqual(
            _defined_authority_names(classes_and_field),
            frozenset({"ProjectDocument", "codec_private_member"}),
        )

    def test_c3_application_consumers_are_exact_and_do_not_open_a_reverse_lane(
        self,
    ) -> None:
        approved = {
            "editor_controller": _SourceModule(
                "editor_controller",
                "from project_package import ProjectPackageService\n",
            ),
            "project_search": _SourceModule(
                "project_search",
                "from project_workspace import ProjectWorkspaceService\n",
            ),
        }
        hostile = {
            "editor_controller": _SourceModule(
                "editor_controller",
                "from project_package import FuturePackageAuthority\n",
            ),
            "project_search": _SourceModule(
                "project_search",
                "import project_save\n",
            ),
            "editor_future_controller": _SourceModule(
                "editor_future_controller",
                "from project_package import ProjectPackageService\n",
            ),
        }

        self.assertEqual(_boundary_violations(approved), ())
        self.assertEqual(
            {
                (item.rule, item.importer, item.imported)
                for item in _boundary_violations(hostile)
            },
            {
                (
                    "application-no-codec-or-future-authority",
                    "editor_controller",
                    "project_package.FuturePackageAuthority",
                ),
                (
                    "application-no-codec-or-future-authority",
                    "project_search",
                    "project_save",
                ),
                (
                    "application-no-codec-or-future-authority",
                    "editor_future_controller",
                    "project_package.ProjectPackageService",
                ),
            },
        )


class MultiDocumentCluster1ProductionArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = _production_modules()
        cls.violations = _boundary_violations(cls.modules)

    def test_future_name_inventory_is_closed_and_duplicate_free(self) -> None:
        self.assertEqual(
            _FUTURE_MODULE_PREFIXES_BY_OWNER,
            _EXPECTED_FUTURE_MODULE_PREFIXES_BY_OWNER,
        )
        self.assertEqual(
            _WORKSPACE_AUTHORITY_NAMES,
            _EXPECTED_WORKSPACE_AUTHORITY_NAMES,
        )
        self.assertEqual(
            len(_ALL_FUTURE_MODULE_PREFIXES),
            len(set(_ALL_FUTURE_MODULE_PREFIXES)),
        )

    def test_cluster1_workspace_production_inventory_is_exact_and_closed(self) -> None:
        observed = {
            name
            for name in self.modules
            if (
                _matches_future_prefix(name)
                or name
                in {
                    _WORKSPACE_ADAPTER_MODULE,
                    _WORKSPACE_SAVE_MODULE,
                    _WORKSPACE_PACKAGE_MODULE,
                }
            )
        }
        self.assertEqual(observed, _CURRENT_WORKSPACE_PRODUCTION_MODULES)

    def test_current_workspace_modules_obey_their_distinct_dependency_rules(self) -> None:
        observed = tuple(
            item
            for item in self.violations
            if item.importer in _CURRENT_WORKSPACE_PRODUCTION_MODULES
        )
        self.assertEqual(observed, ())

    def test_parser_and_codec_define_no_workspace_or_package_authority(self) -> None:
        observed = {
            (name, authority_name)
            for name, module in self.modules.items()
            if _is_parser_or_codec(name)
            for authority_name in _defined_authority_names(module)
        }
        self.assertEqual(observed, set())

    def test_family_scan_reaches_each_current_production_layer(self) -> None:
        self.assertTrue(any(_is_parser_or_codec(name) for name in self.modules))
        self.assertTrue(any(_is_editor_or_application(name) for name in self.modules))
        self.assertTrue(any(_is_qt(name) for name in self.modules))
        self.assertTrue(any(_is_tm_store_or_engine(name) for name in self.modules))

    def test_parser_and_codec_do_not_import_future_workspace_authorities(self) -> None:
        self.assertEqual(
            tuple(
                item
                for item in self.violations
                if item.rule == "parser-no-future-workspace"
            ),
            (),
        )

    def test_editor_and_application_do_not_import_codec_or_future_authorities(
        self,
    ) -> None:
        self.assertEqual(
            tuple(
                item
                for item in self.violations
                if item.rule == "application-no-codec-or-future-authority"
            ),
            (),
        )

    def test_c4_application_workspace_import_surface_is_exact(self) -> None:
        for module_name, expected in _APPLICATION_WORKSPACE_IMPORT_ALLOWLIST.items():
            with self.subTest(module=module_name):
                observed = frozenset(
                    target
                    for target in _collect_import_targets(self.modules[module_name])
                    if _matches_future_prefix(target)
                )
                self.assertEqual(observed, expected)

    def test_qt_does_not_import_parser_manifest_or_package_authorities(self) -> None:
        self.assertEqual(
            tuple(
                item
                for item in self.violations
                if item.rule == "qt-no-parser-manifest-or-package"
            ),
            (),
        )

    def test_tm_store_and_engine_do_not_reverse_import_future_workspace(self) -> None:
        self.assertEqual(
            tuple(
                item
                for item in self.violations
                if item.rule == "tm-no-future-workspace"
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
