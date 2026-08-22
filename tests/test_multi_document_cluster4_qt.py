"""Cluster 4 Qt acceptance over real multi-document ProjectPackages.

These tests intentionally freeze the smallest inspectable Qt surface needed by
the product journeys.  Every multi-document fixture is produced by the real C2
intake/exporter; no manifest, member, or in-memory package double is used.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import cast
from unittest import mock
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QMimeData, Qt, QUrl
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QLabel,
    QMenu,
    QMessageBox,
    QToolButton,
)

from editor_contracts import SearchScope, WorkspaceMode
from editor_controller import EditorController, EditorControllerError
from parser_contracts import (
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
)
from project_package import ProjectPackageService
import project_package as project_package_module
from project_save import (
    DocumentSaveStatus,
    ProjectSaveService,
    RecoveryAction,
    SaveJournalState,
)
from project_workspace import IssuedSegmentIdentity, ProjectWorkspaceService
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    stage_selected_project_documents,
)
from qt_editor import _compose_editor_controller
from qt_editor_window import (
    QtEditorWindow,
    QtWorkspaceCreationDialog,
    QtWorkspacePackageImportDialog,
    _WorkspaceDropPage,
)
from resource_repository import ResourceRepository


_ROOT = Path(__file__).resolve().parents[1]
_PAYLOADS = _ROOT / "tests" / "fixtures" / "parser" / "project" / "payloads"
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _write_json(
    path: Path,
    *,
    name: str,
    segments: tuple[tuple[str, str, str, bool], ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "source_locale": "en",
                "target_locale": "zh-CN",
                "segments": [
                    {
                        "id": local_id,
                        "source": source,
                        "target": target,
                        "speaker": "",
                        "confirmed": confirmed,
                    }
                    for local_id, source, target, confirmed in segments
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _copy_payload(destination: Path, fixture_name: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = _PAYLOADS / fixture_name
    payload = (
        bytes.fromhex(source.read_text(encoding="ascii"))
        if source.suffix == ".hex"
        else source.read_bytes()
    )
    destination.write_bytes(payload)
    return destination


def _export_package(
    source_root: Path,
    selected: tuple[Path, ...],
    destination: Path,
    *,
    name: str,
) -> ProjectPackageService:
    staged = stage_selected_project_documents(
        source_root,
        selected,
        SelectedProjectDocumentsRequest(
            name=name,
            source_locale="en",
            target_locale="zh-CN",
        ),
    )
    workspace_service = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id=f"cluster4-export-{destination.stem}",
        revision=0,
    )
    save_service = ProjectSaveService(workspace_service, baseline=None)
    package_service = ProjectPackageService()
    receipt = package_service.export_workspace(save_service, destination)
    if receipt.receipt is None or not receipt.receipt.durable:
        raise AssertionError("C4 fixture export did not publish a durable package")
    return package_service


class Cluster4QtAcceptanceTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str],
        cast(object, None),
    )
    root: Path = cast(Path, cast(object, None))
    package_path: Path = cast(Path, cast(object, None))
    foreign_package_path: Path = cast(Path, cast(object, None))
    controller: EditorController = cast(EditorController, cast(object, None))
    composition: object = None
    window: QtEditorWindow = cast(QtEditorWindow, cast(object, None))

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="localcat-c4-qt-")
        self.root = Path(self.temporary.name).resolve()

        sources = self.root / "active-sources"
        first = sources / "chapters" / "a.json"
        second = sources / "chapters" / "b.json"
        _write_json(
            first,
            name="Chapter A",
            segments=(
                ("shared", "needle in Chapter A", "A saved", True),
                ("a-tail", "A tail", "", False),
            ),
        )
        _write_json(
            second,
            name="Chapter B",
            segments=(
                ("shared", "needle in Chapter B", "B saved", True),
                ("b-tail", "needle closing", "", False),
            ),
        )
        self.package_path = self.root / "active.localcat-project"
        self.package_service = _export_package(
            sources,
            (first, second),
            self.package_path,
            name="Cluster 4 active project",
        )
        self.nonstandard_package_path = self.root / "remembered-package.bin"
        _export_package(
            sources,
            (first, second),
            self.nonstandard_package_path,
            name="Extension-neutral recent project",
        )

        foreign_sources = self.root / "foreign-sources"
        foreign_first = foreign_sources / "foreign-a.json"
        foreign_second = foreign_sources / "foreign-b.json"
        _write_json(
            foreign_first,
            name="Foreign A",
            segments=(
                ("shared", "DO_NOT_RENDER_FOREIGN_SOURCE_BODY", "", False),
                ("foreign-a", "Foreign A tail", "", False),
            ),
        )
        _write_json(
            foreign_second,
            name="Foreign B",
            segments=(
                ("shared", "Foreign B shared", "", False),
                ("foreign-b", "Foreign B tail", "", False),
            ),
        )
        self.foreign_package_path = self.root / "foreign.localcat-project"
        _export_package(
            foreign_sources,
            (foreign_first, foreign_second),
            self.foreign_package_path,
            name="Foreign safe project name",
        )

        self.controller, self.composition = _compose_editor_controller(
            ResourceRepository(self.root / "app-data")
        )
        self.window = QtEditorWindow(self.controller)
        self.errors: list[tuple[str, str]] = []
        self.window._show_error = lambda title, message: self.errors.append(
            (title, message)
        )
        self.window.show()
        self._events()

    def tearDown(self) -> None:
        self.window._confirm_unsaved = lambda: True
        self.window.close()
        self._events()
        self.temporary.cleanup()

    def _required_method(self, name: str):
        method = getattr(self.window, name, None)
        self.assertTrue(
            callable(method),
            f"Cluster 4 RED: QtEditorWindow.{name} public command is missing",
        )
        return method

    def _open_workspace(self) -> None:
        opened = self._required_method("open_project_package_path")(
            self.package_path
        )
        self.assertTrue(opened)
        self.assertTrue(self.controller.has_workspace)
        self.assertFalse(self.controller.has_project)
        self._events()

    def _document_actions(self) -> tuple[QAction, ...]:
        return tuple(
            action
            for action in self.window.workspace_documents_menu.actions()
            if action.data() is not None
        )

    def _select_document(self, index: int) -> None:
        self._document_actions()[index].trigger()
        self._events()

    def _validate_basic_search(self) -> None:
        owner = self.composition.matcher_validation_owner  # type: ignore[union-attr]
        owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def test_minimal_c4_public_widget_and_command_surface_is_stable(self) -> None:
        widget_contract = (
            ("empty_page", _WorkspaceDropPage, "emptyPage"),
            (
                "workspace_documents_button",
                QToolButton,
                "workspaceDocumentsButton",
            ),
            (
                "workspace_documents_menu",
                QMenu,
                "workspaceDocumentsMenu",
            ),
            ("workspace_chapter_title", QLabel, "workspaceChapterTitle"),
            (
                "workspace_browse_chapter_title",
                QLabel,
                "workspaceBrowseChapterTitle",
            ),
            ("workspace_search_scope", QComboBox, "workspaceSearchScope"),
            ("workspace_save_feedback", QLabel, "workspaceSaveFeedback"),
            (
                "workspace_browse_save_feedback",
                QLabel,
                "workspaceBrowseSaveFeedback",
            ),
        )
        for attribute, expected_type, object_name in widget_contract:
            widget = getattr(self.window, attribute, None)
            self.assertIsInstance(
                widget,
                expected_type,
                f"Cluster 4 RED: {attribute} is missing or has the wrong Qt type",
            )
            self.assertEqual(widget.objectName(), object_name)

        action_contract = (
            ("open_project_action", "openLocalProjectAction"),
            ("import_workspace_package_action", "importWorkspacePackageAction"),
            ("save_workspace_document_action", "saveWorkspaceDocumentAction"),
        )
        for attribute, object_name in action_contract:
            action = getattr(self.window, attribute, None)
            self.assertIsInstance(
                action,
                QAction,
                f"Cluster 4 RED: {attribute} action is missing",
            )
            self.assertEqual(action.objectName(), object_name)

        self.assertEqual(self.window.open_project_action.text(), "打开本地项目")
        self.assertFalse(
            any("…" in action.text() for action in self.window.project_menu.actions())
        )
        self.assertIsNone(getattr(self.window, "new_workspace_project_action", None))
        project_actions = tuple(action.text() for action in self.window.project_menu.actions())
        self.assertFalse(any("单文件" in text for text in project_actions))
        self.assertFalse(any("新建多文档" in text for text in project_actions))
        self.assertIn(
            "导入目标位置",
            self.window.import_workspace_package_action.toolTip(),
        )
        self.assertNotIn(
            "另存为",
            self.window.import_workspace_package_action.toolTip(),
        )
        style = self.window.styleSheet()
        indicator_rule = style.split(
            "QToolButton#workspaceDocumentsButton::menu-indicator",
            1,
        )[1].split("}", 1)[0]
        self.assertIn("image: none", indicator_rule)
        self.assertIn("width: 0px", indicator_rule)
        empty_card = self.window.findChild(QFrame, "emptyCard")
        empty_hint = self.window.findChild(QLabel, "emptyHint")
        self.assertIsNotNone(empty_card)
        self.assertIsNotNone(empty_hint)
        assert empty_card is not None
        assert empty_hint is not None
        self.assertEqual(empty_card.minimumWidth(), 620)
        self.assertEqual(len(empty_hint.text().splitlines()), 2)
        self.assertGreaterEqual(empty_hint.minimumHeight(), 48)
        self.assertFalse(self.window.import_workspace_package_action.isEnabled())
        self.assertTrue(self.window.open_workspace_package_action.isEnabled())
        self.assertIsNone(getattr(self.window, "workspace_transaction_panel", None))
        self.assertIsNone(getattr(self.window, "workspace_import_panel", None))
        self.assertIsNone(getattr(self.window, "workspace_chapter_selector", None))
        self.assertEqual(
            self.window.workspace_documents_button.parent().objectName(),
            "topBar",
        )
        self.assertEqual(
            self.window.workspace_chapter_title.parent().objectName(),
            "segmentPanel",
        )
        self.assertEqual(
            self.window.workspace_browse_chapter_title.parent().objectName(),
            "browsePanel",
        )

        for command in (
            "open_project_package_path",
            "create_workspace_project_from_selected_files",
            "preview_workspace_package_import_path",
            "apply_workspace_package_import",
            "save_workspace_current_document",
            "save_workspace_project_package",
        ):
            self._required_method(command)

        self.assertIs(
            self.window.workspace_save_feedback.textFormat(),
            Qt.TextFormat.PlainText,
        )
        self.assertFalse(self.window.workspace_documents_button.isEnabled())

    def test_import_without_active_workspace_never_bypasses_preview_apply(self) -> None:
        before = self.foreign_package_path.read_bytes()

        self.assertFalse(
            self.window.import_workspace_package_path(self.foreign_package_path)
        )

        self.assertFalse(self.controller.has_active_project)
        self.assertEqual(self.foreign_package_path.read_bytes(), before)
        self.assertIn(
            "PROJECT.PACKAGE.NO_ACTIVE_WORKSPACE",
            self.window.workspace_save_feedback.text(),
        )
        self.assertFalse(self.window._workspace_package_import_can_apply)
        self.assertFalse(self.window.import_workspace_package_action.isEnabled())

    def test_recent_workspace_dispatch_is_typed_and_extension_neutral(self) -> None:
        self.assertTrue(
            self.window.open_project_package_path(self.nonstandard_package_path)
        )
        self.assertTrue(self.controller.has_workspace)
        self.assertTrue(self.window.close_current_project())
        self.assertFalse(self.controller.has_active_project)
        self.assertIn(
            self.nonstandard_package_path,
            tuple(
                item.path for item in self.controller.recent_workspace_projects()
            ),
        )

        self.assertTrue(self.window.open_recent_project(self.nonstandard_package_path))

        self.assertTrue(self.controller.has_workspace)
        self.assertFalse(self.controller.has_project)
        self.assertEqual(
            self.controller.workspace_view.name,
            "Extension-neutral recent project",
        )

    def test_creation_review_preserves_and_explicitly_reorders_selected_files(
        self,
    ) -> None:
        selected = tuple(
            self.root / name
            for name in ("first.json", "second.txt", "third.po", "fourth.pot")
        )
        dialog = QtWorkspaceCreationDialog(
            selected,
            default_name="Explicit order",
            parent=self.window,
        )
        try:
            self.assertEqual(dialog.ordered_paths, selected)
            dialog.selected_files.setCurrentRow(3)
            dialog.move_selected(-1)
            dialog.move_selected(-1)
            self.assertEqual(
                dialog.ordered_paths,
                (selected[0], selected[3], selected[1], selected[2]),
            )
            hint = dialog.findChild(QLabel, "workspaceCreationHint")
            self.assertIsNotNone(hint)
            assert hint is not None
            self.assertIn("不会扫描目录", hint.text())
            self.assertEqual(dialog.source_locale_input.text(), "")
            self.assertEqual(
                dialog.source_locale_input.placeholderText(),
                "默认 en",
            )
            self.assertEqual(dialog.source_locale, "en")
            self.assertEqual(dialog.target_locale_input.text(), "")
            self.assertEqual(
                dialog.target_locale_input.placeholderText(),
                "默认 zh-CN",
            )
            self.assertEqual(dialog.target_locale, "zh-CN")
            self.assertEqual(
                dialog.buttons.button(
                    QDialogButtonBox.StandardButton.Cancel
                ).text(),
                "取消",
            )
            with self.assertRaises(ValueError):
                dialog.move_selected(0)
        finally:
            dialog.close()

    def test_creation_picker_is_suffix_neutral_and_only_forwards_explicit_order(
        self,
    ) -> None:
        root = self.root / "neutral-picker-root"
        selected = (
            root / "first.custom-format",
            root / "second.no-known-suffix",
        )
        destination = self.root / "neutral.localcat-project"
        review = mock.Mock()
        review.exec.return_value = QDialog.DialogCode.Accepted
        review.ordered_paths = selected
        review.project_name_input.text.return_value = "Neutral picker"
        review.source_locale = "en"
        review.target_locale = "zh-CN"

        with (
            mock.patch.object(
                QFileDialog,
                "getOpenFileNames",
                return_value=(tuple(str(path) for path in selected), ""),
            ) as choose_files,
            mock.patch.object(
                QFileDialog,
                "getSaveFileName",
                return_value=(str(destination), ""),
            ),
            mock.patch(
                "qt_editor_window.QtWorkspaceCreationDialog",
                return_value=review,
            ),
            mock.patch.object(
                self.window,
                "create_workspace_from_selected_files",
                return_value=True,
            ) as create,
        ):
            self.assertTrue(self.window._choose_create_workspace())

        self.assertIn("Shift", choose_files.call_args.args[1])
        self.assertEqual(choose_files.call_args.args[2], "")
        self.assertEqual(choose_files.call_args.args[3], "Project documents (*)")
        create.assert_called_once_with(
            root,
            selected,
            destination,
            name="Neutral picker",
            source_locale="en",
            target_locale="zh-CN",
        )

    def test_empty_page_drop_routes_single_or_ordered_explicit_local_files(
        self,
    ) -> None:
        drop_root = self.root / "drop-selection"
        drop_root.mkdir()
        first = drop_root / "first.txt"
        second = drop_root / "second.custom"
        adjacent = drop_root / "not-selected.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        adjacent.write_text("must remain untouched", encoding="utf-8")
        selected = (second, first)

        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(str(path)) for path in selected])
        self.assertEqual(
            self.window.empty_page.explicit_local_file_paths(mime_data),
            selected,
        )

        with mock.patch.object(
            self.window,
            "_review_and_create_workspace_from_selected_paths",
            return_value=True,
        ) as review:
            self.window.empty_page.explicitFilesDropped.emit(selected)
            self._events()
        review.assert_called_once_with(selected)
        self.assertEqual(
            adjacent.read_text(encoding="utf-8"),
            "must remain untouched",
        )

        single_mime = QMimeData()
        single_mime.setUrls([QUrl.fromLocalFile(str(first))])
        self.assertEqual(
            self.window.empty_page.explicit_local_file_paths(single_mime),
            (first,),
        )
        with mock.patch.object(
            self.window,
            "open_project_path",
            return_value=True,
        ) as open_single:
            self.window.empty_page.explicitFilesDropped.emit((first,))
            self._events()
        open_single.assert_called_once_with(first)

        invalid_selections = ((first, first), (first, drop_root))
        for invalid in invalid_selections:
            invalid_mime = QMimeData()
            invalid_mime.setUrls(
                [QUrl.fromLocalFile(str(path)) for path in invalid]
            )
            self.assertIsNone(
                self.window.empty_page.explicit_local_file_paths(invalid_mime)
            )
        remote_mime = QMimeData()
        remote_mime.setUrls(
            [QUrl("https://example.invalid/a.txt"), QUrl.fromLocalFile(str(first))]
        )
        self.assertIsNone(
            self.window.empty_page.explicit_local_file_paths(remote_mime)
        )

    def test_home_open_is_one_control_for_single_and_shift_multi_selection(
        self,
    ) -> None:
        first = self.root / "home-first.txt"
        second = self.root / "home-second.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        self.assertIsNone(getattr(self.window, "empty_workspace_button", None))

        with (
            mock.patch.object(
                QFileDialog,
                "getOpenFileNames",
                return_value=((str(first),), ""),
            ) as choose_single,
            mock.patch.object(
                self.window,
                "open_project_path",
                return_value=True,
            ) as open_single,
        ):
            self.assertTrue(self.window._choose_open_home())
        self.assertIn("Shift", choose_single.call_args.args[1])
        open_single.assert_called_once_with(first)

        with (
            mock.patch.object(
                QFileDialog,
                "getOpenFileNames",
                return_value=((str(second), str(first)), ""),
            ),
            mock.patch.object(
                self.window,
                "_review_and_create_workspace_from_selected_paths",
                return_value=True,
            ) as review,
        ):
            self.assertTrue(self.window._choose_open_home())
        review.assert_called_once_with((second, first))

    def test_real_package_renders_selector_dividers_and_keyboard_cross_chapter_navigation(
        self,
    ) -> None:
        self._open_workspace()
        view = self.controller.workspace_view
        actions = self._document_actions()

        self.assertEqual(len(actions), 2)
        self.assertEqual(
            tuple(action.data() for action in actions),
            tuple(document.identity for document in view.documents),
        )
        self.assertTrue(actions[0].text().startswith("Chapter A"))
        self.assertEqual(actions[1].text(), "Chapter B")
        self.assertTrue(actions[0].text().endswith("✓"))
        self.assertFalse(any(action.text().startswith("1.") for action in actions))
        self.assertTrue(all(not action.icon().isNull() for action in actions))
        self.assertTrue(actions[0].isChecked())
        self.assertFalse(actions[1].isChecked())
        self.assertIn("Chapter A", self.window.workspace_chapter_title.text())
        self.assertNotIn("章节", self.window.workspace_chapter_title.text())

        self.assertEqual(self.window.segment_list.count(), 6)
        expected_rows: tuple[object | None, ...] = (
            None,
            view.segments[0].identity,
            view.segments[1].identity,
            None,
            view.segments[2].identity,
            view.segments[3].identity,
        )
        for row, expected_identity in enumerate(expected_rows):
            item = self.window.segment_list.item(row)
            self.assertIsNotNone(item)
            if expected_identity is None:
                self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsSelectable)
                self.assertFalse(item.icon().isNull())
                self.assertNotIn("章节", item.text())
            else:
                self.assertIs(
                    item.data(Qt.ItemDataRole.UserRole),
                    expected_identity,
                )
        self.assertIn("Chapter A", self.window.segment_list.item(0).text())
        self.assertIn("Chapter B", self.window.segment_list.item(3).text())
        self._select_document(1)
        self.assertEqual(
            self.controller.current_workspace_document_id,
            view.documents[1].identity.document_id,
        )
        self.assertEqual(
            self.controller.current_workspace_identity.local_segment_id,
            "shared",
        )
        self._select_document(0)
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        self._events()
        self.assertEqual(self.controller.workspace_global_index, 2)
        self.assertEqual(
            self.controller.current_workspace_document_id,
            view.documents[1].identity.document_id,
        )
        self.assertEqual(
            self.controller.current_workspace_identity.local_segment_id,
            "shared",
        )
        QTest.mouseClick(self.window.previous_button, Qt.MouseButton.LeftButton)
        self._events()
        self.assertEqual(self.controller.workspace_global_index, 1)
        self.assertEqual(
            self.controller.current_workspace_document_id,
            view.documents[0].identity.document_id,
        )

    def test_explicit_json_txt_po_pot_creation_preserves_selection_order_without_scan(
        self,
    ) -> None:
        portable_root = self.root / "mixed-sources"
        json_path = portable_root / "chapters" / "only.json"
        _write_json(
            json_path,
            name="Selected JSON",
            segments=(("json-id", "JSON selected", "", False),),
        )
        text_path = _copy_payload(
            portable_root / "notes" / "selected.txt",
            "line-text-valid.hex",
        )
        po_path = _copy_payload(
            portable_root / "locale" / "selected.po",
            "gettext-po-valid.po",
        )
        pot_path = _copy_payload(
            portable_root / "locale" / "selected.pot",
            "gettext-pot-valid.pot",
        )
        adjacent = portable_root / "adjacent-not-selected.json"
        adjacent.write_text('{"segments":[', encoding="utf-8")
        adjacent_before = adjacent.read_bytes()
        selected = (pot_path, json_path, text_path, po_path)
        destination = self.root / "mixed.localcat-project"
        request = SelectedProjectDocumentsRequest(
            name="Explicit mixed project",
            source_locale="en",
            target_locale="zh-CN",
        )

        created = self._required_method(
            "create_workspace_project_from_selected_files"
        )(portable_root, selected, request, destination)
        self.assertTrue(created)
        self._events()
        self.assertEqual(adjacent.read_bytes(), adjacent_before)
        self.assertTrue(destination.is_file())

        cold = ProjectPackageService().open(destination).workspace
        self.assertEqual(
            tuple(document.source_ref for document in cold.documents),
            (
                "locale/selected.pot",
                "chapters/only.json",
                "notes/selected.txt",
                "locale/selected.po",
            ),
        )
        self.assertEqual(
            tuple(document.format_id for document in cold.documents),
            (
                GETTEXT_POT_V1.value,
                LOCALCAT_JSON_V1.value,
                LINE_TEXT_V1.value,
                GETTEXT_PO_V1.value,
            ),
        )
        self.assertNotIn(
            "adjacent-not-selected.json",
            tuple(document.source_ref for document in cold.documents),
        )
        self.assertEqual(
            tuple(
                document.display_name
                for document in self.controller.workspace_view.documents
            ),
            tuple(document.display_name for document in cold.documents),
        )
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("LocalCAT项目包已保存", feedback)
        self.assertIn("源文件只读", feedback)

    def test_current_and_entire_project_search_scope_navigate_composite_hits(
        self,
    ) -> None:
        self._validate_basic_search()
        self._open_workspace()
        self._select_document(0)

        scope = self.window.workspace_search_scope
        self.assertEqual(scope.count(), 2)
        self.assertEqual(
            tuple(scope.itemData(index) for index in range(scope.count())),
            (SearchScope.CURRENT_DOCUMENT, SearchScope.ENTIRE_PROJECT),
        )
        self.assertEqual(
            tuple(scope.itemText(index) for index in range(scope.count())),
            ("当前章节", "搜索全部章节"),
        )

        self.window.project_search_input.setText("needle")
        scope.setCurrentIndex(0)
        QTest.mouseClick(
            self.window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        current = self.window.current_workspace_search_report
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.total, 1)
        self.assertEqual(
            tuple(hit.document_id for hit in current.hits),
            (self.controller.workspace_view.documents[0].identity.document_id,),
        )

        scope.setCurrentIndex(1)
        QTest.mouseClick(
            self.window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        entire = self.window.current_workspace_search_report
        self.assertIsNotNone(entire)
        assert entire is not None
        self.assertEqual(entire.total, 3)
        self.assertEqual(
            tuple(hit.project_global_index for hit in entire.hits),
            (0, 2, 3),
        )
        self.assertEqual(
            tuple(
                (hit.document_id, hit.local_segment_id)
                for hit in entire.hits
            ),
            (
                (
                    self.controller.workspace_view.documents[0].identity.document_id,
                    "shared",
                ),
                (
                    self.controller.workspace_view.documents[1].identity.document_id,
                    "shared",
                ),
                (
                    self.controller.workspace_view.documents[1].identity.document_id,
                    "b-tail",
                ),
            ),
        )
        QTest.mouseClick(
            self.window.project_search_next,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(self.controller.workspace_global_index, 2)
        self.assertEqual(
            self.controller.current_workspace_identity.local_segment_id,
            "shared",
        )

    def test_confirm_advances_across_document_boundary_on_issued_identity(self) -> None:
        self._open_workspace()
        self.controller.go_to_workspace_index(
            1,
            project=self.controller.workspace_view.project,
        )
        self.window._refresh_from_controller()
        self.window.target_editor.setPlainText("已确认的 A 章节末段")
        self._events()

        QTest.mouseClick(self.window.confirm_button, Qt.MouseButton.LeftButton)
        self._events()

        confirmed = self.controller.workspace_view.segments[1]
        self.assertTrue(confirmed.confirmed)
        self.assertEqual(confirmed.target, "已确认的 A 章节末段")
        self.assertEqual(self.controller.workspace_global_index, 3)
        self.assertEqual(
            self.controller.current_workspace_identity.local_segment_id,
            "b-tail",
        )

    def test_document_and_full_package_save_feedback_is_body_safe_and_exact_dirty(
        self,
    ) -> None:
        self._open_workspace()
        first_id, second_id = (
            document.identity.document_id
            for document in self.controller.workspace_view.documents
        )
        self.window.target_editor.setPlainText("C4_SECRET_TARGET_A")
        self._events()
        self._select_document(1)
        self.window.target_editor.setPlainText("C4_SECRET_TARGET_B")
        self._events()
        self.assertEqual(
            self.controller.workspace_save_state.dirty_document_ids,
            (first_id, second_id),
        )

        self._select_document(0)
        saved_document = self._required_method(
            "save_workspace_current_document"
        )()
        self.assertTrue(saved_document)
        self._events()
        self.assertEqual(
            self.controller.workspace_save_state.dirty_document_ids,
            (second_id,),
        )
        document_feedback = self.window.workspace_save_feedback.text()
        self.assertIn("LocalCAT项目包已保存", document_feedback)
        self.assertIn("1", document_feedback)
        self.assertIn("源文件只读", document_feedback)
        self.assertIn("Chapter A:saved", document_feedback)
        self.assertIn("Chapter B:unchanged", document_feedback)
        self.assertIn("Chapter B:unchanged·仍未保存", document_feedback)
        actions = self._document_actions()
        self.assertNotIn("未保存", actions[0].text())
        self.assertIn("未保存", actions[1].text())
        self.assertNotIn("C4_SECRET_TARGET_A", document_feedback)
        self.assertNotIn("C4_SECRET_TARGET_B", document_feedback)

        saved_package = self._required_method(
            "save_workspace_project_package"
        )()
        self.assertTrue(saved_package)
        self._events()
        self.assertEqual(self.controller.workspace_save_state.dirty_document_ids, ())
        self.assertFalse(self.controller.workspace_save_state.project_dirty)
        package_feedback = self.window.workspace_save_feedback.text()
        self.assertIn("LocalCAT项目包已保存", package_feedback)
        self.assertIn("2", package_feedback)
        self.assertIn("源文件只读", package_feedback)
        self.assertNotIn("C4_SECRET_TARGET_A", package_feedback)
        self.assertNotIn("C4_SECRET_TARGET_B", package_feedback)

        cold = ProjectPackageService().open(self.package_path).workspace
        self.assertEqual(cold.documents[0].editing_overlay[0].target, "C4_SECRET_TARGET_A")
        self.assertEqual(cold.documents[1].editing_overlay[0].target, "C4_SECRET_TARGET_B")

    def test_save_failure_keeps_session_position_dirty_and_recovery_safe_feedback(
        self,
    ) -> None:
        self._open_workspace()
        self.window.target_editor.setPlainText("dirty survives save failure")
        self._events()
        before_session = self.controller.project_session_id
        before_view = self.controller.workspace_view
        before_identity = self.controller.current_workspace_identity
        before_state = self.controller.workspace_save_state
        before_bytes = self.package_path.read_bytes()

        with mock.patch.object(
            self.controller,
            "save_workspace_package",
            side_effect=EditorControllerError("PROJECT.PACKAGE.APPLY_FAILED"),
        ):
            saved = self._required_method("save_workspace_project_package")()
        self.assertFalse(saved)
        self._events()
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.package_path.read_bytes(), before_bytes)
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("PROJECT.PACKAGE.APPLY_FAILED", feedback)
        self.assertIn("Chapter A", feedback)
        self.assertIn("未保存", feedback)
        self.assertIn("重试", feedback)

    def test_rolled_back_report_never_renders_as_saved(self) -> None:
        self._open_workspace()
        self.window.target_editor.setPlainText("report projection seed")
        self._events()
        committed = self.controller.save_workspace_package()

        rolled_results = tuple(
            replace(
                item,
                status=DocumentSaveStatus.ROLLED_BACK,
                safe_code="PROJECT.SAVE.COMMIT_FAILED",
            )
            for item in committed.save_report.document_results
        )
        rolled_report = replace(
            committed.save_report,
            saved_count=0,
            rolled_back_count=len(rolled_results),
            unchanged_count=0,
            failed_count=0,
            document_results=rolled_results,
            journal_state=SaveJournalState.ROLLED_BACK,
            recovery_required=False,
            retryable=True,
            safe_code="PROJECT.SAVE.COMMIT_FAILED",
        )
        with mock.patch.object(
            self.controller,
            "save_workspace_package",
            return_value=replace(committed, save_report=rolled_report),
        ):
            self.assertFalse(self.window.save_workspace_project_package())
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("PROJECT.SAVE.COMMIT_FAILED", feedback)
        self.assertIn("rolled_back", feedback)
        self.assertNotIn("LocalCAT项目包已保存", feedback)

    def test_real_publication_fault_projects_recovery_and_fresh_service_recovers(
        self,
    ) -> None:
        self._open_workspace()
        self.window.target_editor.setPlainText("real recovery candidate")
        self._events()
        before_session = self.controller.project_session_id
        before_identity = self.controller.current_workspace_identity
        before_document_id = self.controller.current_workspace_document_id
        before_global_index = self.controller.workspace_global_index
        real_unlink = project_package_module._unlink_in_bound_parent
        failed = False

        def fail_first_journal_cleanup(path, expected, **kwargs):
            nonlocal failed
            if "localcat-save-journal-v1" in path.name and not failed:
                failed = True
                raise OSError("DO_NOT_RENDER_RECOVERY_FAULT_BODY")
            return real_unlink(path, expected, **kwargs)

        with mock.patch(
            "project_package._unlink_in_bound_parent",
            side_effect=fail_first_journal_cleanup,
        ):
            self.assertFalse(self.window.save_workspace_project_package())

        self.assertTrue(failed)
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("PROJECT.SAVE.RECOVERY_REQUIRED", feedback)
        self.assertIn("恢复", feedback)
        self.assertNotIn("LocalCAT项目包已保存", feedback)
        self.assertNotIn("DO_NOT_RENDER_RECOVERY_FAULT_BODY", feedback)
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertEqual(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(
            self.controller.current_workspace_document_id,
            before_document_id,
        )
        self.assertEqual(self.controller.workspace_global_index, before_global_index)
        self.assertEqual(
            self.controller.current_workspace_segment.target,
            "real recovery candidate",
        )
        # Publication and candidate readback succeeded before journal cleanup,
        # so the proven baseline is adopted even while cold recovery is required.
        self.assertFalse(self.controller.workspace_save_state.project_dirty)

        fresh = ProjectPackageService()
        preview = fresh.inspect_recovery(self.package_path)
        self.assertIsNotNone(preview)
        assert preview is not None
        self.assertEqual(
            preview.available_actions,
            (RecoveryAction.COMPLETE_COMMIT,),
        )
        recovered = fresh.recover(
            self.package_path,
            preview.operation_id,
            RecoveryAction.COMPLETE_COMMIT,
        )
        self.assertFalse(recovered.recovery_required)
        self.assertIsNone(ProjectPackageService().inspect_recovery(self.package_path))
        cold = ProjectPackageService().open(self.package_path).workspace
        self.assertEqual(
            cold.documents[0].editing_overlay[0].target,
            "real recovery candidate",
        )

    def test_package_preview_requires_explicit_apply_and_success_swaps_after_receipt(
        self,
    ) -> None:
        self._validate_basic_search()
        self._open_workspace()
        active_project_id = self.controller.workspace_view.project.project_id
        foreign_project_id = ProjectPackageService().open(
            self.foreign_package_path
        ).workspace.project_id
        before_session = self.controller.project_session_id
        before_view = self.controller.workspace_view

        previewed = self._required_method(
            "import_workspace_package_path"
        )(self.foreign_package_path)
        self.assertTrue(previewed)
        self._events()
        self.assertIsNone(getattr(self.window, "workspace_import_panel", None))
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            active_project_id,
        )
        preview_text = self.window._workspace_package_preview_text
        self.assertIn("Foreign safe project name", preview_text)
        self.assertIn("2", preview_text)
        self.assertIn("4", preview_text)
        self.assertIn("警告", preview_text)
        self.assertIn("阻断", preview_text)
        self.assertNotIn("DO_NOT_RENDER_FOREIGN_SOURCE_BODY", preview_text)
        self.assertTrue(self.window._workspace_package_import_can_apply)

        self.assertTrue(self.window.apply_workspace_package_import())
        self._events()
        self.assertNotEqual(self.controller.project_session_id, before_session)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            foreign_project_id,
        )
        self.assertEqual(
            ProjectPackageService().open(self.package_path).workspace.project_id,
            foreign_project_id,
        )
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("导入", feedback)
        self.assertIn("receipt", feedback.casefold())
        self.assertNotIn("DO_NOT_RENDER_FOREIGN_SOURCE_BODY", feedback)

        # Continue on the cold-reopened Controller session produced by apply:
        # cross-document navigation, edit, whole-project search, durable save.
        self._select_document(0)
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        self._events()
        self.assertEqual(
            self.controller.current_workspace_document_id,
            self.controller.workspace_view.documents[1].identity.document_id,
        )
        self.window.target_editor.setPlainText("post-import durable target")
        self._events()
        self.window.project_search_input.setText("Foreign")
        self.window.workspace_search_scope.setCurrentIndex(1)
        QTest.mouseClick(
            self.window.project_search_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        report = self.window.current_workspace_search_report
        self.assertIsNotNone(report)
        assert report is not None
        self.assertGreaterEqual(report.total, 3)
        self.assertTrue(self.window.save_workspace_project_package())

        cold_after_journey = ProjectPackageService().open(
            self.package_path
        ).workspace
        self.assertEqual(
            cold_after_journey.documents[1].editing_overlay[0].target,
            "post-import durable target",
        )

    def test_project_menu_import_owns_preview_confirmation_without_top_panel(
        self,
    ) -> None:
        self._open_workspace()
        before_session = self.controller.project_session_id
        with (
            mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(str(self.foreign_package_path), ""),
            ),
            mock.patch.object(
                QtWorkspacePackageImportDialog,
                "exec",
                return_value=QDialog.DialogCode.Accepted,
            ) as confirm,
        ):
            self.assertTrue(self.window._choose_import_workspace_package())
        confirm.assert_called_once()
        self.assertIsNone(getattr(self.window, "workspace_import_panel", None))
        self.assertNotEqual(self.controller.project_session_id, before_session)

    def test_package_import_dialog_explains_replace_and_keeps_full_id_single_line(
        self,
    ) -> None:
        project_id = "prj-" + "d3811369d4b95fda8757b46085e94c907f77267a5dc66833fd8c956e1b5199f60"
        dialog = QtWorkspacePackageImportDialog(
            mode="replace",
            current_project_name="muldoc",
            incoming_project_name="tempDoc",
            incoming_project_id=project_id,
            document_count=2,
            segment_count=27,
            reconciliation_counts=(0, 0, 0, 0, 0, 0),
            warnings=(),
            blocking_reasons=(),
            required_decision_count=0,
            can_apply=True,
            parent=self.window,
        )
        dialog.show()
        self._events()

        self.assertIn("REPLACE", dialog.mode_label.text())
        self.assertEqual(dialog.transition_label.text(), "muldoc  →  tempDoc")
        self.assertIn("不适用", dialog.reconciliation_label.text())
        self.assertIn("不会合并", dialog.reconciliation_label.text())
        self.assertTrue(dialog.project_id_input.isReadOnly())
        self.assertEqual(dialog.project_id_input.text(), project_id)
        self.assertEqual(dialog.project_id_input.toolTip(), project_id)
        self.assertGreaterEqual(dialog.project_id_input.width(), 500)
        self.assertLessEqual(dialog.project_id_input.height(), 40)
        self.assertEqual(dialog.cancel_button.text(), "取消")
        self.assertEqual(dialog.apply_button.text(), "应用导入")
        self.assertTrue(dialog.cancel_button.isDefault())
        self.assertFalse(dialog.apply_button.isDefault())
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.close()

    def test_package_preview_and_apply_failures_preserve_active_session_for_retry(
        self,
    ) -> None:
        self._open_workspace()
        corrupt = self.root / "corrupt.localcat-project"
        corrupt.write_bytes(b"not a project package")
        before_session = self.controller.project_session_id
        before_view = self.controller.workspace_view
        before_identity = self.controller.current_workspace_identity
        before_state = self.controller.workspace_save_state
        before_bytes = self.package_path.read_bytes()

        previewed = self._required_method(
            "preview_workspace_package_import_path"
        )(corrupt)
        self.assertFalse(previewed)
        self._events()
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.package_path.read_bytes(), before_bytes)
        self.assertIn("PROJECT.PACKAGE", self.window.workspace_save_feedback.text())

        self.errors.clear()
        with mock.patch.object(
            self.controller,
            "preview_workspace_package_import",
            side_effect=OSError(
                "DO_NOT_RENDER_RAW_EXCEPTION_BODY /private/secret-project"
            ),
        ):
            self.assertFalse(
                self.window.preview_workspace_package_import_path(corrupt)
            )
        rendered_failure = (
            self.window.workspace_save_feedback.text()
            + repr(self.errors)
        )
        self.assertNotIn("DO_NOT_RENDER_RAW_EXCEPTION_BODY", rendered_failure)
        self.assertNotIn("/private/secret-project", rendered_failure)
        self.assertIn("未显示底层错误内容", rendered_failure)

        self.errors.clear()
        with mock.patch.object(
            self.controller,
            "preview_workspace_package_import",
            side_effect=EditorControllerError("PROJECT.正文"),
        ):
            self.assertFalse(
                self.window.preview_workspace_package_import_path(corrupt)
            )
        rendered_unknown_code = (
            self.window.workspace_save_feedback.text()
            + repr(self.errors)
        )
        self.assertNotIn("PROJECT.正文", rendered_unknown_code)
        self.assertIn("未显示底层错误内容", rendered_unknown_code)

        with mock.patch.object(
            self.controller,
            "preview_workspace_package_import",
            side_effect=TypeError("programmer fault must propagate"),
        ):
            with self.assertRaisesRegex(TypeError, "programmer fault"):
                self.window.preview_workspace_package_import_path(corrupt)

        self.assertTrue(
            self._required_method("preview_workspace_package_import_path")(
                self.foreign_package_path
            )
        )
        with mock.patch.object(
            self.controller,
            "apply_workspace_package_import",
            side_effect=EditorControllerError("PROJECT.PACKAGE.APPLY_FAILED"),
        ):
            applied = self._required_method("apply_workspace_package_import")()
        self.assertFalse(applied)
        self._events()
        self.assertEqual(self.controller.project_session_id, before_session)
        self.assertIs(self.controller.workspace_view, before_view)
        self.assertIs(self.controller.current_workspace_identity, before_identity)
        self.assertEqual(self.controller.workspace_save_state, before_state)
        self.assertEqual(self.package_path.read_bytes(), before_bytes)
        feedback = self.window.workspace_save_feedback.text()
        self.assertIn("PROJECT.PACKAGE.APPLY_FAILED", feedback)
        self.assertIn("重试", feedback)
        self.assertTrue(self.window._workspace_package_import_can_apply)

        self.assertTrue(self.window.apply_workspace_package_import())
        self._events()
        self.assertNotEqual(self.controller.project_session_id, before_session)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            ProjectPackageService().open(
                self.foreign_package_path
            ).workspace.project_id,
        )

    def test_narrow_wide_edit_browse_keep_identity_chapter_and_dirty_feedback(
        self,
    ) -> None:
        self._open_workspace()
        self._select_document(1)
        self.window.target_editor.setPlainText("layout-safe unsaved target")
        self._events()
        identity = self.controller.current_workspace_identity
        state = self.controller.workspace_save_state

        for width, mode in (
            (1080, WorkspaceMode.BROWSE),
            (1440, WorkspaceMode.EDIT),
            (1080, WorkspaceMode.EDIT),
            (1440, WorkspaceMode.BROWSE),
        ):
            self.window.resize(width, 720)
            self.assertTrue(self.window.set_workspace_mode(mode, persist=False))
            self._events()
            self.assertIs(self.controller.current_workspace_identity, identity)
            self.assertEqual(self.controller.workspace_save_state, state)
            if mode is WorkspaceMode.EDIT:
                self.assertIn(
                    "Chapter B",
                    self.window.workspace_chapter_title.text(),
                )
                self.assertTrue(self.window.workspace_documents_button.isVisible())
                self.assertTrue(self.window.workspace_save_feedback.isVisible())
                self.assertFalse(
                    self.window.workspace_browse_chapter_title.isVisible()
                )
            else:
                self.assertIn(
                    "Chapter B",
                    self.window.workspace_browse_chapter_title.text(),
                )
                self.assertTrue(
                    self.window.workspace_browse_save_feedback.isVisible()
                )
                self.assertFalse(
                    self.window.workspace_chapter_title.isVisible()
                )
            self.assertTrue(self.window.workspace_documents_button.isVisible())
            self.assertTrue(self._document_actions()[1].isChecked())
            if width == 1080:
                self.assertLessEqual(
                    self.window.workspace_documents_button.width(),
                    38,
                )
                top_controls = (
                    self.window.progress_bar,
                    self.window.project_search_toggle,
                    self.window.workspace_documents_button,
                    self.window.workspace_mode_combo,
                    self.window.open_button,
                    self.window.save_button,
                    self.window.settings_button,
                )
                visible_controls = tuple(
                    control for control in top_controls if control.isVisible()
                )
                for previous, current in zip(
                    visible_controls,
                    visible_controls[1:],
                ):
                    self.assertLess(
                        previous.geometry().right(),
                        current.geometry().left(),
                        f"top-bar controls overlap: {previous.objectName()} / "
                        f"{current.objectName()}",
                    )

        self.assertEqual(self.window.browse_table.rowCount(), 6)
        self.assertIn("Chapter A", self.window.browse_table.item(0, 0).text())
        self.assertIn("Chapter B", self.window.browse_table.item(3, 0).text())
        self.assertIs(
            self.window.browse_table.item(4, 0).data(Qt.ItemDataRole.UserRole),
            identity,
        )
        self.assertEqual(
            self.controller.current_workspace_segment.target,
            "layout-safe unsaved target",
        )

    def test_browse_current_row_synchronizes_document_title_and_folder_menu(
        self,
    ) -> None:
        self._open_workspace()
        self.assertTrue(
            self.window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        )
        self._events()

        self.window.browse_table.setCurrentCell(4, 1)
        self._events()
        self.assertEqual(self.controller.workspace_global_index, 2)
        self.assertIn(
            "Chapter B",
            self.window.workspace_browse_chapter_title.text(),
        )
        self.assertTrue(self._document_actions()[1].isChecked())
        self.assertFalse(self._document_actions()[0].isChecked())

        self.window.browse_table.setCurrentCell(1, 1)
        self._events()
        self.assertEqual(self.controller.workspace_global_index, 0)
        self.assertIn(
            "Chapter A",
            self.window.workspace_browse_chapter_title.text(),
        )
        self.assertTrue(self._document_actions()[0].isChecked())

        self.window._activate_browse_row(4, 1)
        self._events()
        self.assertIs(self.window.workspace_mode, WorkspaceMode.EDIT)
        self.assertEqual(self.controller.workspace_global_index, 2)
        self.assertTrue(self._document_actions()[1].isChecked())

    def test_browse_grouping_is_per_document_and_jumps_by_issued_identity(
        self,
    ) -> None:
        sources = self.root / "group-sources"
        first = sources / "long-a.json"
        second = sources / "boundary-b.json"
        _write_json(
            first,
            name="Long A",
            segments=tuple(
                (
                    f"a-{index + 1}",
                    f"Long A source {index + 1}",
                    f"Long A target {index + 1}" if index % 2 else "",
                    False,
                )
                for index in range(101)
            ),
        )
        _write_json(
            second,
            name="Boundary B",
            segments=tuple(
                (
                    f"b-{index + 1}",
                    f"Boundary B source {index + 1}",
                    "",
                    False,
                )
                for index in range(121)
            ),
        )
        package = self.root / "browse-groups.localcat-project"
        _export_package(
            sources,
            (first, second),
            package,
            name="Browse groups",
        )
        self.assertTrue(self.window.open_project_package_path(package))
        self.assertTrue(
            self.window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        )
        self._events()

        self.assertEqual(self.window.browse_group_button.text(), "轮次 1 / 6")
        self.assertEqual(
            self.window.browse_group_turn_bar.document_label.toolTip(),
            "Long A",
        )
        self._document_actions()[1].trigger()
        self._events()
        self.assertEqual(self.window.browse_group_button.text(), "轮次 1 / 7")
        self.assertEqual(
            self.window.browse_group_turn_bar.document_label.toolTip(),
            "Boundary B",
        )

        self._document_actions()[0].trigger()
        self._events()

        self.assertTrue(self.window.browse_group_turn_bar.isVisible())
        self.window.browse_group_turn_bar.ticks[5].click()
        self._events()

        self.assertEqual(
            self.controller.current_workspace_identity.local_segment_id,
            "a-101",
        )
        self.assertEqual(self.window.browse_group_button.text(), "轮次 6 / 6")
        self.assertIs(self.window.workspace_mode, WorkspaceMode.BROWSE)

    def test_legacy_single_json_qt_journey_remains_exactly_flat(self) -> None:
        legacy = self.root / "legacy.json"
        saved = self.root / "legacy-saved.json"
        _write_json(
            legacy,
            name="Legacy single JSON",
            segments=(
                ("legacy-a", "Legacy A", "", False),
                ("legacy-b", "Legacy B", "", False),
            ),
        )
        self.assertTrue(self.window.open_project_path(legacy))
        self.assertTrue(self.controller.has_project)
        self.assertFalse(self.controller.has_workspace)
        self.assertEqual(self.window.segment_list.count(), 2)
        self.assertEqual(self.window.project_name_label.text(), "Legacy single JSON")
        self.window.target_editor.setPlainText("旧项目 Qt 译文")
        self._events()
        self.assertTrue(self.controller.dirty)
        self.assertTrue(self.window.save_project_path(saved))
        self.assertFalse(self.controller.dirty)
        self.assertTrue(self.window.close_current_project())
        self.assertTrue(self.window.open_project_path(saved))
        self.assertEqual(self.window.segment_list.count(), 2)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "旧项目 Qt 译文",
        )

    def test_legacy_project_previews_then_imports_package_to_new_destination(
        self,
    ) -> None:
        legacy = self.root / "legacy-import.json"
        destination = self.root / "legacy-import.localcat-project"
        _write_json(
            legacy,
            name="Legacy import source",
            segments=(("legacy", "Legacy source", "Legacy target", True),),
        )
        original = legacy.read_bytes()
        self.assertTrue(self.window.open_project_path(legacy))
        self.assertTrue(self.window.import_workspace_package_action.isEnabled())

        self.assertFalse(
            self.window.import_workspace_package_path(self.foreign_package_path)
        )
        self.assertIn(
            "PROJECT.PACKAGE.DESTINATION_REQUIRED",
            self.window.workspace_save_feedback.text(),
        )
        self.assertTrue(
            self.window.import_workspace_package_path(
                self.foreign_package_path,
                destination=destination,
            )
        )
        self.assertTrue(self.controller.has_project)
        self.assertFalse(self.controller.has_workspace)
        self.assertFalse(destination.exists())
        self.assertEqual(legacy.read_bytes(), original)

        self.assertTrue(self.window.apply_workspace_package_import())
        self.assertTrue(self.controller.has_workspace)
        self.assertFalse(self.controller.has_project)
        self.assertTrue(destination.is_file())
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(
            self.controller.workspace_view.project.project_id,
            self.package_service.open(self.foreign_package_path).workspace.project_id,
        )

    def test_legacy_project_menu_import_chooses_destination_before_apply(self) -> None:
        legacy = self.root / "legacy-menu-import.json"
        destination = self.root / "legacy-menu-import.localcat-project"
        _write_json(
            legacy,
            name="Legacy menu import",
            segments=(("legacy", "Legacy source", "", False),),
        )
        original = legacy.read_bytes()
        self.assertTrue(self.window.open_project_path(legacy))

        with (
            mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(str(self.foreign_package_path), ""),
            ),
            mock.patch.object(
                QFileDialog,
                "getSaveFileName",
                return_value=(str(destination), ""),
            ) as choose_destination,
            mock.patch.object(
                QtWorkspacePackageImportDialog,
                "exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
        ):
            self.assertTrue(self.window._choose_import_workspace_package())

        choose_destination.assert_called_once()
        self.assertIn("保存为", choose_destination.call_args.args[1])
        self.assertTrue(self.controller.has_workspace)
        self.assertTrue(destination.is_file())
        self.assertEqual(legacy.read_bytes(), original)

    def test_qt_source_does_not_import_package_parser_or_private_carrier_authority(
        self,
    ) -> None:
        source_path = Path(__import__("qt_editor_window").__file__).resolve()
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)
        forbidden_imports = {
            "parser_application",
            "parser_composition",
            "parser_contracts",
            "project_package",
            "project_workspace_intake",
        }
        self.assertEqual(imported_modules & forbidden_imports, set())
        for forbidden_marker in (
            "codec_private_member",
            "ProjectPackageManifest",
            "manifest.json",
            "ZipFile",
            "getExistingDirectory",
            "Project documents (*.json",
        ):
            self.assertNotIn(forbidden_marker, source)


if __name__ == "__main__":
    unittest.main()
