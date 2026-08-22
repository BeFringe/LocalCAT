"""Cluster 0 current-source baseline for the future project workspace.

This file deliberately characterizes the brownfield single-document product.
Later clusters must update the closed inventories when authority moves; they
must not weaken the guards merely to keep this baseline green.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import cast
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ProjectSearchHit,
    ProjectSearchRequest,
    SearchField,
)
from editor_controller import EditorController, EditorControllerError
from editor_project import ProjectError, load_project, save_project
from parser_composition import create_parser_application_surface
from parser_contracts import (
    CanonicalDocumentWrite,
    CanonicalSegmentWrite,
    CanonicalSerializeRequest,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    ParsedSegment,
    RawSpeaker,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
)
from qt_editor import _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tm_contracts import SearchOptions


_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_ROOT = _ROOT / "tests" / "fixtures" / "parser" / "project" / "payloads"
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)

_CURRENT_SOURCE_ROOTS = (
    "editor_project",
    "editor_controller",
    "project_search",
    "workspace_state",
    "parser_source",
    "qt_editor",
    "qt_editor_window",
    "editor_contracts",
    "parser_contracts",
    "parser_composition",
)
_CURRENT_SOURCE_FILES = tuple(f"{module}.py" for module in _CURRENT_SOURCE_ROOTS)

_EXPECTED_PARSER_DOCUMENT_FACTS = {
    "localcat-json-v1": {
        "event_types": ("DocumentHeader", "ParsedSegment"),
        "header": ("Chapter One", "en-US", "zh-CN", ()),
        "segments": (
            (
                "one",
                "First source",
                "",
                "explicit_empty",
                "unconfirmed",
                "",
                (),
            ),
        ),
        "terminal": (1, (), False, 0, "localcat-json", "1", "localcat-json-v1"),
    },
    "line-text-v1": {
        "event_types": (
            "DocumentHeader",
            "ParsedSegment",
            "ParsedSegment",
            "ParsedSegment",
        ),
        "header": ("line-text-valid", None, None, ()),
        "segments": (
            ("segment-1", "First line", None, "missing", None, "", ()),
            ("segment-2", "Second  line", None, "missing", None, "", ()),
            ("segment-3", "Third line", None, "missing", None, "", ()),
        ),
        "terminal": (3, (), False, 0, "line-text", "1", "line-text-v1"),
    },
    "gettext-po-v1": {
        "event_types": ("DocumentHeader", "ParsedSegment"),
        "header": (
            "gettext-po-valid",
            None,
            None,
            (
                (
                    "gettext.header",
                    "Content-Type: text/plain; charset=UTF-8\nLanguage: zh_CN\n",
                ),
            ),
        ),
        "segments": (
            (
                "entry-6-1",
                "Hello world",
                "你好",
                "present",
                "format_derived_unconfirmed",
                "",
                (
                    ("gettext.comments", ("#. Synthetic translator note",)),
                    ("gettext.references", ("#: chapter.rpy:10",)),
                    ("gettext.flags", ("#, fuzzy",)),
                    (
                        "gettext.previous_values",
                        ('#| msgid "Old menu label"',),
                    ),
                    ("gettext.msgctxt", "menu"),
                ),
            ),
        ),
        "terminal": (1, (), False, 0, "gettext-po", "1", "gettext-po-v1"),
    },
    "gettext-pot-v1": {
        "event_types": ("DocumentHeader", "ParsedSegment"),
        "header": (
            "gettext-pot-valid",
            None,
            None,
            (("gettext.header", "Content-Type: text/plain; charset=UTF-8\n"),),
        ),
        "segments": (
            (
                "entry-5-1",
                "Start game",
                "",
                "explicit_empty",
                None,
                "",
                (
                    ("gettext.comments", ("#. Synthetic template entry",)),
                    ("gettext.references", ("#: screens.rpy:5",)),
                    ("gettext.msgctxt", "button"),
                ),
            ),
        ),
        "terminal": (1, (), False, 0, "gettext-pot", "1", "gettext-pot-v1"),
    },
}

_RUNTIME_SOURCE_DIGESTS = {
    "editor_project.py": "a9ebff92b352e3db7d2d68098937215e92d40e2f7984a5d3a64451bb5782798a",
    "editor_controller.py": "5861c52affc8cac37038e585756e59b8de1d15bce9479251f94e0ef86500b841",
    "project_search.py": "8dba7726eb5c5438bbd37b66d58650cf83daac30d823f5f32e117ecda9f4d673",
    "workspace_state.py": "efa66ddaf73bbcd9dc13729777041805804ddc11e0cec300279129962f0585cb",
    "parser_source.py": "db6e94ff8424d57212eaf901b2b8c1eb6cc321b62cf8767498859f04d5121783",
    "qt_editor.py": "dff5462184491cd0525e4efb4979ec9189ed32c9a04c53db5e2125b698ebc7d5",
    "qt_editor_window.py": "83878e5fe4c2e9cb9ce291f8a29c6443cbaeb33c28cf3862b44a8c3c9c67ae4e",
    "editor_contracts.py": "7ade4ad624b372fb02881ab6807deadf10c649092c1f00e34933f83859e7dca9",
    "parser_contracts.py": "36bdbbe30b111034ac3a73889b7b897de1d2086fe494ff562d20c0b88116a600",
    "parser_composition.py": "afa777612e8149acec0ee68c8f8e689a1dc1eab376668419a63a1ac71d673e7e",
}

_PYTHON_SOURCE_ENTRY_COUNT = 260
_PYTHON_SOURCE_PATH_DIGEST = (
    "976edc2309a62619bdd52390bdde57eb9affed914134141bdb0cc3914ca1ae80"
)
_PRODUCTION_IMPORT_ENTRY_COUNT = 463
_PRODUCTION_IMPORT_CALL_COUNT = 463
_PRODUCTION_IMPORT_DIGEST = (
    "3838fe5f7e4978e0906d0a4a4f65334b32ec7c32dfdfa335d2fe534086c92ec4"
)
_TEST_IMPORT_ENTRY_COUNT = 816
_TEST_IMPORT_CALL_COUNT = 1002
_TEST_IMPORT_DIGEST = (
    "8a46da5dfbc8c6d9548bc9bb832ef363aad29726f68e09e621e1d68c678005ff"
)
_CRITICAL_PRODUCTION_IMPORTS = frozenset(
    {
        ("editor_project.py", "editor_contracts", "EditorProject", None),
        (
            "editor_project.py",
            "parser_composition",
            "create_parser_application_surface",
            None,
        ),
        ("editor_project.py", "parser_contracts", "CanonicalDocumentWrite", None),
        ("editor_controller.py", "editor_project", "load_project", None),
        ("editor_controller.py", "project_search", "ProjectSearchService", None),
        (
            "editor_controller.py",
            "workspace_state",
            "WorkspaceStateRepository",
            None,
        ),
        ("project_search.py", "editor_contracts", "ProjectSearchHit", None),
        ("workspace_state.py", "editor_contracts", "RecentProject", None),
        ("parser_source.py", "parser_contracts", "SourceReference", None),
        (
            "parser_composition.py",
            "parser_source",
            "create_sealed_snapshot",
            "_create_sealed_snapshot",
        ),
        (
            "parser_composition.py",
            "parser_contracts",
            "CanonicalSerializeRequest",
            None,
        ),
        ("qt_editor.py", "editor_controller", "EditorController", None),
        ("qt_editor.py", "qt_editor_window", "QtEditorWindow", None),
        ("qt_editor_window.py", "editor_contracts", "ProjectSearchRequest", None),
        ("qt_editor_window.py", "editor_controller", "EditorController", None),
        (
            "project_workspace_contracts.py",
            "parser_contracts",
            "CodecIdentity",
            None,
        ),
        (
            "project_workspace_contracts.py",
            "project_workspace_identity",
            "normalize_portable_ref_v1",
            None,
        ),
        (
            "editor_project_workspace_adapter.py",
            "editor_contracts",
            "EditorProject",
            None,
        ),
        (
            "editor_project_workspace_adapter.py",
            "parser_composition",
            "create_parser_application_surface",
            None,
        ),
        (
            "editor_project_workspace_adapter.py",
            "parser_contracts",
            "LOCALCAT_JSON_V1",
            None,
        ),
        (
            "editor_project_workspace_adapter.py",
            "project_workspace_contracts",
            "ProjectWorkspace",
            None,
        ),
        (
            "editor_project_workspace_adapter.py",
            "project_workspace_identity",
            "derive_legacy_single_json_project_id",
            None,
        ),
    }
)

_KEY_CONSTRUCTORS = frozenset(
    {
        "CanonicalDocumentWrite",
        "CanonicalSegmentWrite",
        "CanonicalSerializeRequest",
        "EditorController",
        "EditorProject",
        "EditorSegment",
        "ProjectSearchRequest",
        "ProjectSearchService",
        "ParserApplicationSurface",
        "OpenedParserInput",
        "PreparedCanonicalWrite",
        "QtEditorWindow",
        "ReadRequest",
        "SelectionRequest",
        "SourceReference",
        "TargetReference",
        "WorkspaceStateRepository",
    }
)
_EXPECTED_CONSTRUCTOR_CALLS = Counter(
    {
        ("editor_controller.py", "EditorController"): 1,
        ("editor_controller.py", "ProjectSearchRequest"): 1,
        ("editor_controller.py", "ProjectSearchService"): 1,
        ("editor_controller.py", "WorkspaceStateRepository"): 1,
        ("editor_project.py", "CanonicalDocumentWrite"): 1,
        ("editor_project.py", "CanonicalSegmentWrite"): 1,
        ("editor_project.py", "CanonicalSerializeRequest"): 1,
        ("editor_project.py", "EditorProject"): 2,
        ("editor_project.py", "EditorSegment"): 4,
        ("editor_project.py", "ReadRequest"): 1,
        ("editor_project.py", "SelectionRequest"): 1,
        ("editor_project.py", "SourceReference"): 1,
        ("editor_project.py", "TargetReference"): 1,
        ("parser_composition.py", "OpenedParserInput"): 1,
        ("parser_composition.py", "ParserApplicationSurface"): 1,
        ("parser_composition.py", "PreparedCanonicalWrite"): 1,
        ("parser_composition.py", "SelectionRequest"): 1,
        ("qt_editor.py", "EditorController"): 1,
        ("qt_editor.py", "QtEditorWindow"): 1,
        ("qt_editor_window.py", "ProjectSearchRequest"): 1,
    }
)
_KEY_AUTHORITY_CALLS = frozenset(
    {
        "create_parser_application_surface",
        "find_project",
        "_atomic_write_bytes",
        "_create_sealed_snapshot",
        "_materialize",
        "_validate",
        "create_canonical_serializer",
        "create_reader",
        "create_sealed_snapshot",
        "load_project",
        "open_input",
        "open_project",
        "prepare_canonical",
        "remember_project",
        "save_project",
        "save_project_file",
        "search",
        "serialize_canonical",
        "stream",
        "update_target",
        "verified_terminal",
        "write",
    }
)
_EXPECTED_AUTHORITY_CALLS = Counter(
    {
        ("editor_controller.py", "load_project"): 1,
        ("editor_controller.py", "save_project_file"): 1,
        ("editor_controller.py", "search"): 1,
        ("editor_controller.py", "self.update_target"): 3,
        ("editor_controller.py", "self.workspace_state.find_project"): 1,
        ("editor_controller.py", "self.workspace_state.remember_project"): 1,
        ("editor_project.py", "create_parser_application_surface"): 2,
        ("editor_project.py", "opened.stream"): 1,
        ("editor_project.py", "prepared.write"): 1,
        ("editor_project.py", "session.verified_terminal"): 1,
        ("editor_project.py", "surface.open_input"): 1,
        ("editor_project.py", "surface.prepare_canonical"): 1,
        ("parser_composition.py", "_atomic_write_bytes"): 1,
        ("parser_composition.py", "_create_sealed_snapshot"): 2,
        ("parser_composition.py", "_materialize"): 1,
        ("parser_composition.py", "_validate"): 1,
        (
            "parser_composition.py",
            "self._registry.create_canonical_serializer",
        ): 1,
        ("parser_composition.py", "self._registry.create_reader"): 3,
        ("parser_composition.py", "self.prepare_canonical"): 1,
        ("parser_composition.py", "serializer.serialize_canonical"): 1,
        ("parser_composition.py", "write"): 1,
        ("parser_source.py", "create_sealed_snapshot"): 1,
        ("parser_source.py", "os.write"): 1,
        ("parser_source.py", "session.verified_terminal"): 2,
        ("qt_editor.py", "controller.open_project"): 1,
        ("qt_editor_window.py", "self.controller.open_project"): 1,
        ("qt_editor_window.py", "self.controller.save_project"): 1,
        ("qt_editor_window.py", "self.controller.update_target"): 1,
        ("workspace_state.py", "handle.write"): 1,
    }
)
_KEY_SERIALIZATION_CALLS = frozenset(
    {
        "_read_state",
        "_write_state",
        "dumps",
        "loads",
        "read_bytes",
        "read_text",
        "write_bytes",
        "write_text",
    }
)
_EXPECTED_SERIALIZATION_CALLS = Counter(
    {
        ("editor_contracts.py", "json.dumps"): 1,
        ("editor_contracts.py", "json.loads"): 1,
        ("editor_controller.py", "json.dumps"): 2,
        ("editor_controller.py", "json.loads"): 1,
        ("editor_controller.py", "path.read_text"): 1,
        ("qt_editor.py", "json.dumps"): 1,
        ("qt_editor.py", "temporary.write_text"): 1,
        ("qt_editor.py", "temporary_path.write_text"): 1,
        ("workspace_state.py", "json.dumps"): 1,
        ("workspace_state.py", "json.loads"): 1,
        ("workspace_state.py", "self._read_state"): 1,
        ("workspace_state.py", "self._write_state"): 5,
        ("workspace_state.py", "self.state_path.read_text"): 1,
    }
)
_PATCH_ENTRY_COUNT = 36
_PATCH_CALL_COUNT = 51
_PATCH_INVENTORY_DIGEST = (
    "c1999224063bcc127f6e074b79203ed3ef4db62908d111cb2806edf63d7920bf"
)

_INVENTORY_MODULES = frozenset(_CURRENT_SOURCE_ROOTS)
_C1_CONSUMER_MODULES = frozenset(
    {
        "project_workspace_contracts",
        "project_workspace_identity",
        "editor_project_workspace_adapter",
    }
)
_CLOSED_CONSUMER_MODULES = _INVENTORY_MODULES | _C1_CONSUMER_MODULES
_SEMANTIC_SOURCE_FILES = _CURRENT_SOURCE_FILES


def _tree(relative: str, *, source: str | None = None) -> ast.Module:
    text = (_ROOT / relative).read_text(encoding="utf-8") if source is None else source
    return ast.parse(text, filename=relative)


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _selected_calls(
    relative: str,
    names: frozenset[str],
    *,
    source: str | None = None,
) -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    for node in ast.walk(_tree(relative, source=source)):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted.rsplit(".", 1)[-1] in names:
            observed[(relative, dotted)] += 1
    return observed


def _semantic_call_inventory(
    *,
    override: tuple[str, str] | None = None,
) -> tuple[
    Counter[tuple[str, str]],
    Counter[tuple[str, str]],
    Counter[tuple[str, str]],
]:
    constructors: Counter[tuple[str, str]] = Counter()
    authority: Counter[tuple[str, str]] = Counter()
    serialization: Counter[tuple[str, str]] = Counter()
    for relative in _SEMANTIC_SOURCE_FILES:
        source = override[1] if override is not None and override[0] == relative else None
        constructors.update(_selected_calls(relative, _KEY_CONSTRUCTORS, source=source))
        authority.update(_selected_calls(relative, _KEY_AUTHORITY_CALLS, source=source))
        serialization.update(
            _selected_calls(relative, _KEY_SERIALIZATION_CALLS, source=source)
        )
    return constructors, authority, serialization


def _recursive_python_sources() -> tuple[Path, ...]:
    """Close the scan over every non-hidden Python source in this worktree."""

    observed: list[Path] = []
    for path in _ROOT.rglob("*.py"):
        relative = path.relative_to(_ROOT)
        if any(
            part == "__pycache__" or part.startswith(".")
            for part in relative.parts
        ):
            continue
        observed.append(path)
    return tuple(
        sorted(observed, key=lambda path: path.relative_to(_ROOT).as_posix())
    )


def _source_path_digest(paths: tuple[Path, ...]) -> str:
    payload = tuple(path.relative_to(_ROOT).as_posix() for path in paths)
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _import_consumers(
    paths: tuple[Path, ...],
) -> Counter[tuple[str, str, str, str | None]]:
    observed: Counter[tuple[str, str, str, str | None]] = Counter()
    for path in paths:
        relative = path.relative_to(_ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), relative)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in _CLOSED_CONSUMER_MODULES
            ):
                for item in node.names:
                    observed[
                        (relative, cast(str, node.module), item.name, item.asname)
                    ] += 1
            elif isinstance(node, ast.Import):
                for item in node.names:
                    if item.name in _CLOSED_CONSUMER_MODULES:
                        observed[(relative, item.name, "<module>", item.asname)] += 1
    return observed


def _import_counter_digest(
    counter: Counter[tuple[str, str, str, str | None]],
) -> str:
    payload = sorted(
        (relative, module, name, alias or "", count)
        for (relative, module, name, alias), count in counter.items()
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _patch_inventory(
    *,
    extra_source: tuple[str, str] | None = None,
) -> Counter[tuple[str, str]]:
    observed: Counter[tuple[str, str]] = Counter()
    sources = [
        (path.relative_to(_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in _recursive_python_sources()
        if path.relative_to(_ROOT).parts[0] == "tests"
    ]
    if extra_source is not None:
        sources.append(extra_source)
    for relative, source in sources:
        for node in ast.walk(ast.parse(source, filename=relative)):
            if not isinstance(node, ast.Call):
                continue
            call_name = _dotted_name(node.func).rsplit(".", 1)[-1]
            if call_name != "patch":
                continue
            for argument in node.args:
                if not (
                    isinstance(argument, ast.Constant)
                    and type(argument.value) is str
                ):
                    continue
                target = cast(str, argument.value)
                if any(
                    target == module or target.startswith(f"{module}.")
                    for module in _CLOSED_CONSUMER_MODULES
                ):
                    observed[(relative, target)] += 1
    return observed


def _counter_digest(counter: Counter[tuple[str, str]]) -> str:
    payload = sorted(
        (relative, symbol, count)
        for (relative, symbol), count in counter.items()
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_checked_in_fixture(name: str, target: Path, *, hex_encoded: bool = False) -> None:
    source = _FIXTURE_ROOT / name
    payload = source.read_bytes()
    target.write_bytes(bytes.fromhex(payload.decode("ascii")) if hex_encoded else payload)


def _metadata_facts(entries: object) -> tuple[tuple[str, object], ...]:
    return tuple((entry.key, entry.value) for entry in cast(tuple[object, ...], entries))


def _parser_document_facts(path: Path, format_id: object) -> dict[str, object]:
    surface = create_parser_application_surface()
    selection = SelectionRequest(
        purpose=EffectivePurpose.PROJECT_DOCUMENT,
        format_id=format_id,
    )
    opened = surface.open_input(
        SourceReference(
            safe_root=str(path.parent),
            selected_path=str(path),
            display_hint=path.name,
        ),
        selection,
        ReadRequest(
            purpose=EffectivePurpose.PROJECT_DOCUMENT,
            format_id=format_id,
        ),
    )
    if isinstance(opened, SelectionFailure):
        raise AssertionError(f"fixture selection failed: {opened.code}")
    with opened:
        session = opened.stream()
        try:
            events = tuple(session)
            terminal = session.verified_terminal()
        finally:
            session.close()
    headers = tuple(event for event in events if type(event) is DocumentHeader)
    segments = tuple(event for event in events if type(event) is ParsedSegment)
    if len(headers) != 1:
        raise AssertionError("project fixture must issue exactly one header")
    header = headers[0]
    return {
        "event_types": tuple(type(event).__name__ for event in events),
        "header": (
            header.name,
            header.source_locale,
            header.target_locale,
            _metadata_facts(header.metadata),
        ),
        "segments": tuple(
            (
                segment.local_id,
                segment.source,
                segment.target,
                segment.target_presence.value,
                (
                    segment.translation_state.value
                    if segment.translation_state is not None
                    else None
                ),
                segment.speaker.value,
                _metadata_facts(segment.format_metadata),
            )
            for segment in segments
        ),
        "terminal": (
            terminal.record_count,
            tuple(
                (warning.code, warning.severity.value, warning.count)
                for warning in terminal.warning_counts
            ),
            terminal.issues_truncated,
            terminal.fatal_count,
            terminal.codec_identity.codec_id,
            terminal.codec_identity.codec_version,
            terminal.limit_profile.profile_id,
        ),
    }


def _search_request(query: str) -> ProjectSearchRequest:
    return ProjectSearchRequest(
        query=query,
        fields=(SearchField.SOURCE,),
        options=SearchOptions(match_case=False, whole_word=False),
    )


class MultiDocumentCluster0SourceInventoryTests(unittest.TestCase):
    def test_current_runtime_roots_imports_calls_patches_and_serializers_are_closed(
        self,
    ) -> None:
        self.assertEqual(
            tuple(_RUNTIME_SOURCE_DIGESTS),
            _CURRENT_SOURCE_FILES,
        )
        self.assertEqual(_INVENTORY_MODULES, frozenset(_CURRENT_SOURCE_ROOTS))
        self.assertTrue(_INVENTORY_MODULES.isdisjoint(_C1_CONSUMER_MODULES))
        self.assertEqual(
            _CLOSED_CONSUMER_MODULES,
            _INVENTORY_MODULES | _C1_CONSUMER_MODULES,
        )
        observed_digests = {
            relative: hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()
            for relative in _RUNTIME_SOURCE_DIGESTS
        }
        self.assertEqual(observed_digests, _RUNTIME_SOURCE_DIGESTS)

        all_sources = _recursive_python_sources()
        self.assertEqual(len(all_sources), _PYTHON_SOURCE_ENTRY_COUNT)
        self.assertEqual(_source_path_digest(all_sources), _PYTHON_SOURCE_PATH_DIGEST)
        production_sources = tuple(
            path
            for path in all_sources
            if path.relative_to(_ROOT).parts[0] != "tests"
        )
        test_sources = tuple(
            path
            for path in all_sources
            if path.relative_to(_ROOT).parts[0] == "tests"
        )
        production_imports = _import_consumers(production_sources)
        test_imports = _import_consumers(test_sources)
        self.assertEqual(len(production_imports), _PRODUCTION_IMPORT_ENTRY_COUNT)
        self.assertEqual(
            sum(production_imports.values()),
            _PRODUCTION_IMPORT_CALL_COUNT,
        )
        self.assertEqual(
            _import_counter_digest(production_imports),
            _PRODUCTION_IMPORT_DIGEST,
        )
        self.assertTrue(
            _CRITICAL_PRODUCTION_IMPORTS.issubset(production_imports),
            _CRITICAL_PRODUCTION_IMPORTS.difference(production_imports),
        )
        self.assertEqual(len(test_imports), _TEST_IMPORT_ENTRY_COUNT)
        self.assertEqual(sum(test_imports.values()), _TEST_IMPORT_CALL_COUNT)
        self.assertEqual(
            _import_counter_digest(test_imports),
            _TEST_IMPORT_DIGEST,
        )
        constructors, authority, serialization = _semantic_call_inventory()
        self.assertEqual(constructors, _EXPECTED_CONSTRUCTOR_CALLS)
        self.assertEqual(authority, _EXPECTED_AUTHORITY_CALLS)
        self.assertEqual(serialization, _EXPECTED_SERIALIZATION_CALLS)

        patches = _patch_inventory()
        self.assertEqual(len(patches), _PATCH_ENTRY_COUNT)
        self.assertEqual(sum(patches.values()), _PATCH_CALL_COUNT)
        self.assertEqual(_counter_digest(patches), _PATCH_INVENTORY_DIGEST)
        for seam in (
            (
                "tests/test_parser_project_facade_characterization.py",
                "editor_project.create_parser_application_surface",
            ),
            ("tests/test_parser_wave4_safety.py", "parser_source.os.replace"),
            ("tests/test_workspace_state.py", "workspace_state.os.replace"),
            ("tests/test_qt_bootstrap.py", "qt_editor.sys.platform"),
            (
                "tests/test_multi_document_cluster1_contracts.py",
                "editor_project_workspace_adapter.create_parser_application_surface",
            ),
        ):
            self.assertGreater(patches[seam], 0, seam)

    def test_inventory_helpers_reject_representative_authority_and_patch_mutations(
        self,
    ) -> None:
        relative = "editor_project.py"
        source = (_ROOT / relative).read_text(encoding="utf-8")
        mutated = source.replace("surface.open_input(", "surface.open_workspace_input(", 1)
        self.assertNotEqual(mutated, source)
        _constructors, authority, _serialization = _semantic_call_inventory(
            override=(relative, mutated)
        )
        self.assertNotEqual(authority, _EXPECTED_AUTHORITY_CALLS)

        current_patches = _patch_inventory()
        mutated_patches = _patch_inventory(
            extra_source=(
                "synthetic_mutation.py",
                'from unittest.mock import patch\npatch("editor_project.load_project")\n',
            )
        )
        self.assertNotEqual(
            _counter_digest(mutated_patches),
            _counter_digest(current_patches),
        )

        current_sources = _recursive_python_sources()
        nested_mutation = _ROOT / "synthetic" / "nested" / "consumer.py"
        self.assertNotIn(nested_mutation, current_sources)
        self.assertNotEqual(
            _source_path_digest((*current_sources, nested_mutation)),
            _source_path_digest(current_sources),
        )


class MultiDocumentCluster0PublicJourneyTests(unittest.TestCase):
    def test_four_project_formats_have_exact_parser_terminal_facts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c0-parser-") as temporary:
            text_path = Path(temporary) / "line-text-valid.txt"
            _copy_checked_in_fixture(
                "line-text-valid.hex",
                text_path,
                hex_encoded=True,
            )
            matrix = (
                (_FIXTURE_ROOT / "localcat-object-valid.json", LOCALCAT_JSON_V1),
                (text_path, LINE_TEXT_V1),
                (_FIXTURE_ROOT / "gettext-po-valid.po", GETTEXT_PO_V1),
                (_FIXTURE_ROOT / "gettext-pot-valid.pot", GETTEXT_POT_V1),
            )
            observed = {
                format_id.value: _parser_document_facts(path, format_id)
                for path, format_id in matrix
            }

        self.assertEqual(observed, _EXPECTED_PARSER_DOCUMENT_FACTS)

    def test_single_json_controller_journey_is_flat_searchable_and_cold_reopenable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c0-json-") as temporary:
            root = Path(temporary)
            source_path = root / "chapter.json"
            saved_path = root / "saved.json"
            _copy_checked_in_fixture("localcat-array-valid.json", source_path)
            repository = ResourceRepository(root / "app-data")
            controller, composition = _compose_editor_controller(repository)
            _ = composition.matcher_validation_owner.validate_basic(
                generated_at_utc=_GENERATED_AT,
                valid_until_utc=_VALID_UNTIL,
                evaluated_at_utc=_EVALUATED_AT,
            )

            opened = controller.open_project(source_path)
            report = controller.search_project(_search_request("Hello"))
            controller.update_target("已编辑")
            controller.go_to(1)
            controller.update_target("第二条译文")

            self.assertEqual(
                tuple(field.name for field in fields(EditorProject)),
                ("name", "segments", "source_locale", "target_locale", "path"),
            )
            self.assertEqual(
                tuple(field.name for field in fields(ProjectSearchHit)),
                (
                    "segment_id",
                    "segment_index",
                    "field",
                    "start_index",
                    "end_index",
                    "preview",
                ),
            )
            self.assertEqual(tuple(segment.id for segment in opened.segments), ("intro", "segment-2"))
            self.assertEqual(
                tuple((hit.segment_id, hit.segment_index) for hit in report.hits),
                (("intro", 0),),
            )
            self.assertTrue(controller.dirty)

            saved = controller.save_project(saved_path)
            self.assertFalse(controller.dirty)
            self.assertEqual(saved.path, saved_path.absolute())
            payload = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(
                tuple(segment["id"] for segment in payload["segments"]),
                ("intro", "segment-2"),
            )

            reopened, _composition = _compose_editor_controller(
                ResourceRepository(root / "app-data")
            )
            cold = reopened.open_project(saved_path)

            self.assertEqual(reopened.current_index, 1)
            self.assertEqual(reopened.current_segment.id, "segment-2")
            self.assertEqual(cold.segments[0].target, "已编辑")
            self.assertEqual(cold.segments[1].target, "第二条译文")
            self.assertFalse(reopened.dirty)
            state = json.loads(
                (root / "app-data" / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["schema_version"], 1)
            self.assertEqual(
                set(state),
                {
                    "schema_version",
                    "recent_projects",
                    "display",
                    "tm_preferences",
                    "preprocessing",
                },
            )
            self.assertNotIn("source", state)
            self.assertNotIn("target", state)

    def test_txt_is_source_only_search_gated_and_can_only_be_saved_as_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c0-txt-") as temporary:
            root = Path(temporary)
            text_path = root / "chapter.txt"
            saved_path = root / "converted.json"
            _copy_checked_in_fixture("line-text-valid.hex", text_path, hex_encoded=True)
            controller, _composition = _compose_editor_controller(
                ResourceRepository(root / "app-data")
            )

            project = controller.open_project(text_path)
            self.assertEqual(len(project.segments), 3)
            self.assertTrue(all(segment.target == "" for segment in project.segments))
            self.assertTrue(all(not segment.confirmed for segment in project.segments))
            self.assertEqual(
                controller.project_tool_capability().unavailable_reason,
                "PROJECT_TOOLS.JSON_REQUIRED",
            )

            controller.update_target("暂存译文")
            self.assertTrue(controller.dirty)
            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT_TOOLS\.JSON_REQUIRED$",
            ):
                controller.search_project(_search_request("First"))
            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT\.SAVE_FAILED$",
            ):
                controller.save_project(root / "cannot-write.txt")
            self.assertTrue(controller.dirty)

            controller.save_project(saved_path)
            self.assertFalse(controller.dirty)
            cold = load_project(saved_path)
            self.assertEqual(cold.segments[0].target, "暂存译文")
            self.assertEqual(cold.path, saved_path.absolute())

    def test_po_and_pot_are_parser_readable_but_absent_from_editor_entry_and_writer(
        self,
    ) -> None:
        surface = create_parser_application_surface()
        document = CanonicalDocumentWrite(
            name="Reader only",
            source_locale="en-US",
            target_locale="zh-CN",
            segments=(
                CanonicalSegmentWrite(
                    local_id="one",
                    source="Source",
                    target="Target",
                    speaker=RawSpeaker(""),
                    confirmed=False,
                ),
            ),
        )
        matrix = (
            ("gettext-po-valid.po", GETTEXT_PO_V1),
            ("gettext-pot-valid.pot", GETTEXT_POT_V1),
        )
        with tempfile.TemporaryDirectory(prefix="localcat-c0-gettext-") as temporary:
            root = Path(temporary)
            valid = root / "active.json"
            _copy_checked_in_fixture("localcat-array-valid.json", valid)
            state_path = root / "app-data" / "workspace.json"
            controller = EditorController(ResourceRepository(root / "app-data"))
            controller.open_project(valid)
            controller.go_to(1)
            controller.update_target("尚未保存")
            project_before = controller.project
            session_before = controller.project_session_id
            index_before = controller.current_index
            dirty_before = controller.dirty
            state_before = state_path.read_bytes()
            self.assertEqual(index_before, 1)
            self.assertTrue(dirty_before)

            for fixture_name, format_id in matrix:
                with self.subTest(format_id=format_id.value):
                    path = _FIXTURE_ROOT / fixture_name
                    selection = SelectionRequest(
                        purpose=EffectivePurpose.PROJECT_DOCUMENT,
                        format_id=format_id,
                    )
                    descriptor = surface.select(selection)
                    self.assertNotIsInstance(descriptor, SelectionFailure)
                    assert not isinstance(descriptor, SelectionFailure)
                    self.assertTrue(descriptor.capabilities.readable)
                    self.assertFalse(descriptor.capabilities.canonical_write)

                    with self.assertRaisesRegex(
                        ProjectError,
                        rf"^unsupported project format: \{path.suffix}$",
                    ):
                        load_project(path)
                    with self.assertRaisesRegex(
                        EditorControllerError,
                        r"^PROJECT\.LOAD_FAILED$",
                    ):
                        controller.open_project(path)
                    self.assertIs(controller.project, project_before)
                    self.assertEqual(controller.project_session_id, session_before)
                    self.assertEqual(controller.current_index, index_before)
                    self.assertEqual(controller.dirty, dirty_before)
                    self.assertEqual(state_path.read_bytes(), state_before)

                    with self.assertRaises(ContractViolation) as caught:
                        surface.prepare_canonical(
                            EffectivePurpose.PROJECT_DOCUMENT,
                            CanonicalSerializeRequest(
                                format_id=format_id,
                                document=document,
                            ),
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
                    )

        json_descriptor = surface.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                format_id=LOCALCAT_JSON_V1,
            )
        )
        text_descriptor = surface.select(
            SelectionRequest(
                purpose=EffectivePurpose.PROJECT_DOCUMENT,
                format_id=LINE_TEXT_V1,
            )
        )
        assert not isinstance(json_descriptor, SelectionFailure)
        assert not isinstance(text_descriptor, SelectionFailure)
        self.assertTrue(json_descriptor.capabilities.canonical_write)
        self.assertFalse(text_descriptor.capabilities.canonical_write)
        prepared = surface.prepare_canonical(
            EffectivePurpose.PROJECT_DOCUMENT,
            CanonicalSerializeRequest(
                format_id=LOCALCAT_JSON_V1,
                document=document,
            ),
        )
        self.assertIsNotNone(prepared)

    def test_failed_controller_open_and_save_are_body_safe_and_preserve_session(
        self,
    ) -> None:
        secret_body = "DO-NOT-LEAK-SOURCE-BODY"
        with tempfile.TemporaryDirectory(prefix="localcat-c0-fault-") as temporary:
            root = Path(temporary)
            valid = root / "valid.json"
            invalid = root / "secret-name.json"
            _copy_checked_in_fixture("localcat-array-valid.json", valid)
            invalid.write_text(
                f'{{"segments":[{{"source":"{secret_body}"}}',
                encoding="utf-8",
            )
            controller = EditorController(ResourceRepository(root / "app-data"))
            controller.open_project(valid)
            controller.go_to(1)
            controller.update_target("未保存")
            before = controller.project
            session_id = controller.project_session_id
            current_index = controller.current_index
            state_path = root / "app-data" / "workspace.json"
            state_bytes = state_path.read_bytes()

            with self.assertRaises(EditorControllerError) as open_error:
                controller.open_project(invalid)
            self.assertEqual(open_error.exception.args, ("PROJECT.LOAD_FAILED",))
            self.assertNotIn(secret_body, str(open_error.exception))
            self.assertNotIn(str(invalid), str(open_error.exception))
            self.assertIs(controller.project, before)
            self.assertEqual(controller.project_session_id, session_id)
            self.assertEqual(controller.current_index, current_index)
            self.assertTrue(controller.dirty)
            self.assertEqual(state_path.read_bytes(), state_bytes)

            with self.assertRaises(EditorControllerError) as save_error:
                controller.save_project(root / "wrong.txt")
            self.assertEqual(save_error.exception.args, ("PROJECT.SAVE_FAILED",))
            self.assertIs(controller.project, before)
            self.assertEqual(controller.project_session_id, session_id)
            self.assertEqual(controller.current_index, current_index)
            self.assertTrue(controller.dirty)
            self.assertEqual(state_path.read_bytes(), state_bytes)


class MultiDocumentCluster0QtSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_qt_public_session_remains_one_flat_project_across_save_reopen_and_fault(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c0-qt-") as temporary:
            root = Path(temporary)
            source_path = root / "chapter.json"
            saved_path = root / "saved.json"
            invalid_path = root / "invalid.json"
            _copy_checked_in_fixture("localcat-array-valid.json", source_path)
            invalid_path.write_text('{"segments":[', encoding="utf-8")
            window = QtEditorWindow(
                EditorController(ResourceRepository(root / "app-data"))
            )
            errors: list[tuple[str, str]] = []
            window._show_error = lambda title, message: errors.append((title, message))
            try:
                self.assertTrue(window.open_project_path(source_path))
                self.assertEqual(window.segment_list.count(), 2)
                self.assertEqual(window.project_name_label.text(), "chapter")

                window.target_editor.setPlainText("来自 Qt 的译文")
                self._events()
                self.assertTrue(window.controller.dirty)
                self.assertEqual(
                    window.controller.current_segment.target,
                    "来自 Qt 的译文",
                )
                self.assertTrue(window.save_project_path(saved_path))
                self.assertFalse(window.controller.dirty)

                self.assertTrue(window.close_current_project())
                self.assertFalse(window.controller.has_project)
                self.assertTrue(window.open_project_path(saved_path))
                self.assertEqual(window.segment_list.count(), 2)
                self.assertEqual(
                    window.controller.project.segments[0].target,
                    "来自 Qt 的译文",
                )
                session_before_fault = window.controller.project_session_id
                project_before_fault = window.controller.project
                self.assertFalse(window.open_project_path(invalid_path))
                self.assertEqual(errors[-1][1], "PROJECT.LOAD_FAILED")
                self.assertIs(window.controller.project, project_before_fault)
                self.assertEqual(
                    window.controller.project_session_id,
                    session_before_fault,
                )
            finally:
                window._confirm_unsaved = lambda: True
                window.close()
                self._events()


if __name__ == "__main__":
    unittest.main()
