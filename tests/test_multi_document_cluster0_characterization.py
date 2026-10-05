"""直接验证 Workspace 架构与既有单文档产品行为。

源码边界由当前 AST 和行为断言检查，不固定源码摘要、文件数量或调用次数。
"""

from __future__ import annotations

import ast
from dataclasses import fields
from datetime import datetime, timezone
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
from tests.parser_architecture_test_support import (
    collect_import_references,
    collect_imported_calls,
)


_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_ROOT = _ROOT / "tests" / "fixtures" / "parser" / "project" / "payloads"
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)

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
        (
            "project_save.py",
            "project_workspace",
            "ProjectWorkspaceService",
            None,
        ),
        (
            "project_save.py",
            "project_workspace_contracts",
            "ProjectWorkspace",
            None,
        ),
        (
            "project_package.py",
            "project_save",
            "ProjectSaveService",
            None,
        ),
        (
            "project_package.py",
            "project_workspace",
            "ProjectWorkspaceService",
            None,
        ),
        ("workspace_state.py", "editor_contracts", "RecentProject", None),
        ("parser_source.py", "parser_contracts", "SourceReference", None),
        (
            "parser_composition.py",
            "parser_source",
            "create_sealed_snapshot",
            "_create_sealed_snapshot",
        ),
        (
            "project_workspace_intake.py",
            "parser_composition",
            "create_parser_application_surface",
            None,
        ),
        (
            "project_workspace_intake.py",
            "project_workspace_contracts",
            "StagedSelectedProjectDocuments",
            None,
        ),
        (
            "project_workspace.py",
            "project_workspace_contracts",
            "StagedSelectedProjectDocuments",
            None,
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

_WORKSPACE_FORBIDDEN_IMPORTS = {
    "project_workspace_identity.py": (
        "parser_", "editor_", "qt_", "project_save", "project_package",
        "workspace_state", "resource_", "tm_", "collaborative_",
    ),
    "project_workspace_contracts.py": (
        "parser_composition", "parser_source", "parser_registry", "parser_localcat_codec",
        "editor_", "qt_", "project_save", "project_package", "resource_", "tm_", "collaborative_",
    ),
    "editor_project_workspace_adapter.py": (
        "parser_source", "parser_registry", "parser_localcat_codec",
        "project_package", "resource_", "tm_", "qt_", "PySide6",
    ),
    "project_workspace_intake.py": (
        "parser_source", "parser_registry", "parser_localcat_codec",
        "project_package", "resource_", "tm_", "qt_", "PySide6",
    ),
    "project_workspace.py": (
        "parser_", "project_save", "project_package", "editor_", "qt_",
        "resource_", "tm_", "collaborative_", "PySide6",
    ),
    "project_save.py": (
        "parser_", "project_package", "editor_", "qt_", "resource_", "tm_",
        "collaborative_", "PySide6",
    ),
}
_CARRIER_NEUTRAL_SOURCES = ("project_workspace.py", "project_save.py")
_DIRECT_IO_CALLS = frozenset({
    "open", "read_bytes", "read_text", "write_bytes", "write_text",
    "iterdir", "rglob", "glob", "scandir", "listdir", "replace", "rename", "unlink", "fsync",
})
_FOREIGN_SERIALIZATION_PREFIXES = ("json.", "pickle.", "csv.", "sqlite3.", "zipfile.", "xml.")


def _workspace_boundary_violations(relative: str, source: str) -> tuple[str, ...]:
    module_name = Path(relative).stem
    forbidden = _WORKSPACE_FORBIDDEN_IMPORTS[relative]
    violations = [
        item.target
        for item in collect_import_references(source, module_name=module_name)
        if item.target.startswith(forbidden)
    ]
    if relative in _CARRIER_NEUTRAL_SOURCES:
        imported_calls = collect_imported_calls(source, module_name=module_name)
        violations.extend(
            target
            for target, _line, _column in imported_calls
            if target.startswith(_FOREIGN_SERIALIZATION_PREFIXES)
            or target.startswith(("os.", "io.", "pathlib."))
            and target.rsplit(".", 1)[-1] in _DIRECT_IO_CALLS
        )
        tree = ast.parse(source, filename=relative)
        path_constructions = {
            (line, column)
            for target, line, column in imported_calls
            if target in {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}
        }
        path_variables = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and (node.value.lineno, node.value.col_offset) in path_constructions
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                violations.append("open")
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _DIRECT_IO_CALLS - {"replace"}
            ):
                violations.append(node.func.attr)
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
                receiver = node.func.value
                if (
                    isinstance(receiver, ast.Call)
                    and (receiver.lineno, receiver.col_offset) in path_constructions
                ) or isinstance(receiver, ast.Name) and receiver.id in path_variables:
                    violations.append("pathlib.Path.replace")
    return tuple(violations)


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


class MultiDocumentArchitectureTests(unittest.TestCase):
    def test_required_owner_composition_remains_direct(self) -> None:
        # These edges connect the public API to the approved owner. Aliases and
        # additional benign calls are irrelevant to the dependency contract.
        for relative, module, symbol, _alias in _CRITICAL_PRODUCTION_IMPORTS:
            with self.subTest(source=relative, owner=module, symbol=symbol):
                imports = collect_import_references(
                    (_ROOT / relative).read_text(encoding="utf-8"),
                    module_name=Path(relative).stem,
                )
                self.assertIn(f"{module}.{symbol}", {item.target for item in imports})

    def test_workspace_owners_do_not_acquire_parser_carrier_or_storage_authority(self) -> None:
        for relative in _WORKSPACE_FORBIDDEN_IMPORTS:
            with self.subTest(source=relative):
                source = (_ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(_workspace_boundary_violations(relative, source), ())

    def test_dependency_guard_rejects_aliased_and_dynamic_foreign_owners(self) -> None:
        for relative, forbidden in _WORKSPACE_FORBIDDEN_IMPORTS.items():
            for prefix in forbidden:
                module = prefix + "foreign" if prefix.endswith("_") else prefix
                for source in (
                    f"import {module} as dependency\n",
                    f"from {module} import foreign as dependency\n",
                    f"from importlib import import_module as load\nload('{module}')\n",
                ):
                    with self.subTest(owner=relative, source=source):
                        self.assertTrue(_workspace_boundary_violations(relative, source))

    def test_carrier_neutral_owners_cannot_open_publish_or_serialize_files(self) -> None:
        for relative in _CARRIER_NEUTRAL_SOURCES:
            for source in (
                "open('project.json', 'w')",
                "path.write_bytes(b'project')",
                "path.read_text()",
                "import os\nos.replace(source, destination)",
                "import json as wire\nwire.dumps({})",
                "from json import dumps as encode\nencode({})",
                "from os import fsync as flush\nflush(fd)",
            ):
                with self.subTest(owner=relative, source=source):
                    self.assertTrue(_workspace_boundary_violations(relative, source))
            self.assertEqual(
                _workspace_boundary_violations(
                    relative,
                    '# open("project")\nnote = "json.dumps"\n'
                    'note.replace("json", "wire")\n'
                    'import dataclasses\ndataclasses.replace(value, target="new")\n',
                ),
                (),
            )

    def test_path_replace_is_publication_but_string_and_dataclass_replace_are_not(self) -> None:
        for source in (
            "from pathlib import Path\nPath('old').replace('new')",
            "from pathlib import Path\np = Path('old')\np.replace('new')",
            "from pathlib import Path as FilePath\nFilePath('old').replace('new')",
            "import pathlib as fs\np = fs.Path('old')\np.replace('new')",
        ):
            with self.subTest(source=source):
                self.assertTrue(_workspace_boundary_violations("project_save.py", source))
        self.assertEqual(
            _workspace_boundary_violations(
                "project_save.py",
                'name = "old"\nname.replace("old", "new")\n'
                'import dataclasses\ndataclasses.replace(value, target="new")\n',
            ),
            (),
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
