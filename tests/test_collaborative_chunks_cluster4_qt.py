"""Cluster 4 end-to-end Qt acceptance over one real ProjectPackage.

The fixture is produced through the real multi-document intake/export path.
Chunk metadata is likewise published by the application facade; this test does
not fabricate a package member, manifest, metadata payload, or store snapshot.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QWidget

from chunk_controller_contracts import (
    ChunkApplicationMode,
    CollaborativeSearchScopeV2,
    CollaborativeWorkspaceSearchReportV2,
)
from editor_contracts import (
    LegacyExactTMSuggestion,
    SearchScope,
    TermSuggestion,
    WorkspaceMode,
)
from editor_controller import EditorController, EditorControllerError
from project_package import ProjectPackageService
from project_save import ProjectSaveService
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import SegmentIdentity
from project_workspace_intake import (
    SelectedProjectDocumentsRequest,
    revalidate_staged_selected_documents,
    stage_selected_project_documents,
)
from qt_chunk_manager_dialog import QtChunkManagerDialog
from qt_browse_group_dialog import BrowseGroupPreview
from qt_editor import _compose_chunk_controller, _compose_editor_controller
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository


_ROOT = Path(__file__).resolve().parents[1]
_GENERATED_AT = datetime(2030, 1, 1, tzinfo=timezone.utc)
_VALID_UNTIL = datetime(2030, 1, 2, tzinfo=timezone.utc)
_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _write_source(
    path: Path,
    *,
    name: str,
    rows: tuple[tuple[str, str], ...],
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
                        "target": "",
                        "speaker": "",
                        "confirmed": False,
                    }
                    for local_id, source in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _export_real_package(
    source_root: Path,
    selected: tuple[Path, ...],
    destination: Path,
) -> None:
    staged = stage_selected_project_documents(
        source_root,
        selected,
        SelectedProjectDocumentsRequest(
            name="Chunk C4 Qt",
            source_locale="en",
            target_locale="zh-CN",
        ),
    )
    workspace = ProjectWorkspaceService(
        staged.workspace,
        staged.origin_binding,
        session_id="chunk-c4-qt-export",
        revision=0,
    )
    result = ProjectPackageService().export_workspace(
        ProjectSaveService(workspace, baseline=None),
        destination,
    )
    if result.receipt is None or not result.receipt.durable:
        raise AssertionError("real C4 ProjectPackage export was not durable")


class CollaborativeChunkCluster4QtAcceptanceTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str] = cast(
        tempfile.TemporaryDirectory[str], cast(object, None)
    )
    root: Path = cast(Path, cast(object, None))
    app_data: Path = cast(Path, cast(object, None))
    package_path: Path = cast(Path, cast(object, None))
    controller: EditorController = cast(EditorController, cast(object, None))
    chunk_controller: object = None
    window: QtEditorWindow = cast(QtEditorWindow, cast(object, None))

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    @staticmethod
    def _enable_matcher(composition: object) -> None:
        composition.matcher_validation_owner.validate_basic(
            generated_at_utc=_GENERATED_AT,
            valid_until_utc=_VALID_UNTIL,
            evaluated_at_utc=_EVALUATED_AT,
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chunk-c4-qt-")
        self.root = Path(self.temporary.name).resolve()
        self.app_data = self.root / "app-data"
        sources = self.root / "sources"
        first = sources / "alpha.json"
        second = sources / "beta.json"
        self.sources = sources
        self.first_source = first
        self.second_source = second
        _write_source(
            first,
            name="Alpha document",
            rows=(
                ("shared", "needle alpha member"),
                ("tail", "outside-only needle"),
            ),
        )
        _write_source(
            second,
            name="Beta document",
            rows=(
                ("shared", "needle beta member"),
                ("tail", "second chunk member"),
            ),
        )
        self.package_path = self.root / "chunk-c4.localcat-project"
        _export_real_package(
            sources,
            (first, second),
            self.package_path,
        )
        self.package_bytes = self.package_path.read_bytes()

        repository = ResourceRepository(self.app_data)
        self.controller, composition = _compose_editor_controller(repository)
        self._enable_matcher(composition)
        self.chunk_controller = _compose_chunk_controller(
            self.controller,
            repository,
        )
        self.window = QtEditorWindow(
            self.controller,
            chunk_controller=self.chunk_controller,
        )
        self.errors: list[tuple[str, str]] = []
        self.window._show_error = lambda title, message: self.errors.append(
            (title, message)
        )
        self.window.show()
        self._events()
        self.assertTrue(self.window.open_project_package_path(self.package_path))
        self._events()

    def tearDown(self) -> None:
        self.window._confirm_unsaved = lambda: True
        self.window.close()
        self.window.deleteLater()
        self._events()
        self.temporary.cleanup()

    def _plain_identity(self, document_index: int, local_id: str) -> SegmentIdentity:
        document = self.controller.workspace_view.documents[document_index]
        return SegmentIdentity(document.identity.document_id, local_id)

    def _issued_identity(self, document_index: int, local_id: str):
        expected = self._plain_identity(document_index, local_id)
        return next(
            item.identity
            for item in self.controller.workspace_view.segments
            if item.identity.segment_identity == expected
        )

    def _go_to(self, document_index: int, local_id: str) -> None:
        self.controller.go_to_workspace_segment(
            self._issued_identity(document_index, local_id)
        )
        self.window._render_current_segment()
        self._events()

    def _select_scope(self, scope: object) -> None:
        index = self.window.workspace_search_scope.findData(scope)
        self.assertGreaterEqual(index, 0)
        self.window.workspace_search_scope.setCurrentIndex(index)

    def _search(self, query: str):
        self.window.project_search_source.setChecked(True)
        self.window.project_search_input.setText(query)
        self.window._submit_project_search()
        self._events()
        report = self.window.current_project_search_report
        self.assertIsInstance(report, CollaborativeWorkspaceSearchReportV2)
        return report

    @staticmethod
    def _identity_value(value: object) -> SegmentIdentity | None:
        nested = getattr(value, "segment_identity", None)
        if type(nested) is SegmentIdentity:
            return nested
        return value if type(value) is SegmentIdentity else None

    def _manager(self) -> QtChunkManagerDialog:
        view = self.chunk_controller.project_view()
        dialog = QtChunkManagerDialog(
            self.chunk_controller,
            view,
            self.window,
        )
        dialog.show()
        self._events()
        self.assertIs(dialog._facade, self.chunk_controller)
        self.assertTrue(dialog.isVisible())
        return dialog

    def _choose_manager_action(
        self,
        dialog: QtChunkManagerDialog,
        action: str,
    ) -> None:
        index = dialog.action_combo.findData(action)
        if index >= 0:
            dialog.action_combo.setCurrentIndex(index)
        else:
            dialog.advanced_button.setChecked(True)
            self._events()
            index = dialog.advanced_combo.findData(action)
            self.assertGreaterEqual(index, 0, action)
            dialog.advanced_combo.setCurrentIndex(index)
        self._events()

    def _select_manager_chunks(
        self,
        dialog: QtChunkManagerDialog,
        names: tuple[str, ...],
    ) -> None:
        rows = {
            dialog.chunk_table.item(row, 0).text(): row
            for row in range(dialog.chunk_table.rowCount())
        }
        self.assertTrue(set(names).issubset(rows), (names, rows))
        dialog.chunk_table.clearSelection()
        flags = (
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows
        )
        selection_model = dialog.chunk_table.selectionModel()
        for name in names:
            selection_model.select(
                dialog.chunk_table.model().index(rows[name], 0),
                flags,
            )
        self._events()

    def _set_group_names(
        self,
        count_widget: object,
        names_widget: object,
        names: tuple[str, ...],
    ) -> None:
        count_widget.setValue(len(names))
        self._events()
        self.assertEqual(names_widget.count(), len(names))
        for index, name in enumerate(names):
            names_widget.item(index).setText(name)
        self._events()

    def _publish_manager(self, dialog: QtChunkManagerDialog) -> None:
        self.assertTrue(dialog.preview_button.isEnabled())
        dialog.preview_button.setFocus()
        self.assertIs(dialog.focusWidget(), dialog.preview_button)
        QTest.keyClick(dialog.preview_button, Qt.Key.Key_Return)
        self._events()
        self.assertTrue(dialog.confirm_check.isEnabled(), dialog.preview_panel.text())
        dialog.confirm_check.setChecked(True)
        self.assertTrue(dialog.apply_button.isEnabled(), dialog.preview_panel.text())
        dialog.apply_button.setFocus()
        QTest.keyClick(dialog.apply_button, Qt.Key.Key_Return)
        self._events()
        self.assertIn("已发布", dialog.preview_panel.text())

    def _close_manager(self, dialog: QtChunkManagerDialog) -> None:
        dialog.close()
        dialog.deleteLater()
        self._events()

    @staticmethod
    def _menu_action(menu: QMenu, text: str):
        return next((action for action in menu.actions() if action.text() == text), None)

    def test_real_package_manager_split_merge_undo_scope_and_cold_reopen(self) -> None:
        # NO_PLAN stays editable, but collaboration is not a fixed home-page bar.
        initial = self.chunk_controller.project_view()
        self.assertIs(initial.mode, ChunkApplicationMode.NO_PLAN)
        self.assertTrue(initial.current_segment_access.may_edit_target)
        self.assertFalse(self.window.target_editor.isReadOnly())
        self.assertTrue(self.window.confirm_button.isEnabled())
        self.assertIsNone(self.window.findChild(QWidget, "chunkBar"))
        self.assertFalse(hasattr(self.window, "chunk_bar"))
        manage_action = self._menu_action(self.window.project_menu, "协作分工管理")
        current_action = self._menu_action(self.window.project_menu, "当前分工")
        self.assertIsNotNone(manage_action)
        self.assertIsNotNone(current_action)
        self.assertIsNotNone(current_action.menu())
        self.assertTrue(self.window.workspace_documents_button.isEnabled())
        self.assertFalse(self.window.workspace_documents_button.icon().isNull())
        self.assertEqual(len(self.window.workspace_documents_menu.actions()), 2)
        self.assertEqual(
            [
                self.window.workspace_search_scope.itemText(index)
                for index in range(self.window.workspace_search_scope.count())
            ],
            ["当前章节", "搜索全部章节"],
        )
        self.assertEqual(
            self.window.workspace_search_scope.itemData(0),
            SearchScope.CURRENT_DOCUMENT.value,
        )

        # NO_PLAN directly partitions the whole Project: no empty Chunk and no
        # editor-row selection are prerequisites.  Exercise the narrow-but-
        # supported dialog layout and keyboard publication path at the same time.
        dialog = self._manager()
        try:
            self.assertEqual(dialog.action_combo.count(), 1)
            self.assertEqual(dialog.action_combo.currentData(), "partition")
            self.assertEqual(dialog._selected_segments(), ())
            dialog.resize(860, 620)
            self._events()
            self.assertEqual(len(dialog.body_splitter.sizes()), 2)
            self.assertTrue(all(size > 200 for size in dialog.body_splitter.sizes()))
            for widget in (
                dialog.action_combo,
                dialog.preview_button,
                dialog.apply_button,
            ):
                self.assertTrue(widget.accessibleName())
                self.assertNotEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
            self._set_group_names(
                dialog.partition_group_count,
                dialog.partition_group_names,
                ("Alpha batch", "Beta batch"),
            )
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)

        active = self.chunk_controller.project_view()
        self.assertIs(active.mode, ChunkApplicationMode.ACTIVE)
        self.assertEqual(
            [(chunk.name, chunk.member_count) for chunk in active.chunks],
            [("Alpha batch", 2), ("Beta batch", 2)],
        )
        self.assertEqual(active.unallocated_count, 0)

        # Assignment and the simple split are both manager-owned.  Split uses
        # only source Chunk + group count/names + an explicit assignment rule.
        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "assign")
            self._select_manager_chunks(dialog, ("Alpha batch",))
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)
        self.assertTrue(
            next(
                chunk
                for chunk in self.chunk_controller.project_view().chunks
                if chunk.name == "Alpha batch"
            ).assigned_to_current_reference
        )

        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "split_evenly")
            self._select_manager_chunks(dialog, ("Alpha batch",))
            self.assertEqual(
                {
                    dialog.split_assignment.itemData(index)
                    for index in range(dialog.split_assignment.count())
                },
                {None, "inherit", "unassign"},
            )
            self._set_group_names(
                dialog.split_group_count,
                dialog.split_group_names,
                ("Alpha front", "Alpha back"),
            )
            dialog.split_assignment.setCurrentIndex(
                dialog.split_assignment.findData("inherit")
            )
            self.assertEqual(dialog._selected_segments(), ())
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)
        split_view = self.chunk_controller.project_view()
        self.assertEqual(
            [(chunk.name, chunk.member_count) for chunk in split_view.chunks],
            [("Alpha front", 1), ("Alpha back", 1), ("Beta batch", 2)],
        )
        self.assertTrue(
            all(
                chunk.assigned_to_current_reference
                for chunk in split_view.chunks[:2]
            )
        )

        # Merge one split child with the other Document's Chunk.  This creates
        # the cross-document membership used by both editor projections below.
        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "merge")
            self._select_manager_chunks(dialog, ("Alpha front", "Beta batch"))
            dialog.merge_name.setText("Cross document")
            dialog.merge_assignment.setCurrentIndex(
                dialog.merge_assignment.findData(True)
            )
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)
        merged_view = self.chunk_controller.project_view()
        merged = next(
            chunk for chunk in merged_view.chunks if chunk.name == "Cross document"
        )
        self.assertEqual(merged.member_count, 3)
        self.assertTrue(merged.assigned_to_current_reference)

        # v1 never resurrects retired identities: a retiring split/merge head
        # is explicitly not undoable.  The negative path must remain visible
        # and non-publishable in the same real manager.
        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "undo")
            QTest.keyClick(dialog.preview_button, Qt.Key.Key_Return)
            self._events()
            self.assertIn("CHUNK.UNDO_UNAVAILABLE", dialog.preview_panel.text())
            self.assertFalse(dialog.confirm_check.isEnabled())
            self.assertFalse(dialog.apply_button.isEnabled())
        finally:
            self._close_manager(dialog)

        # Project-menu selection is the only persistent entry.  It projects
        # exact cross-document membership into both Edit and Browse/Review.
        self.window.project_menu.aboutToShow.emit()
        self._events()
        current_action = self._menu_action(self.window.project_menu, "当前分工")
        self.assertIsNotNone(current_action)
        current_menu = current_action.menu()
        self.assertIsNotNone(current_menu)
        select_merged = next(
            action
            for action in current_menu.actions()
            if action.data() == merged.chunk_id
        )
        select_merged.trigger()
        self._events()
        self.assertEqual(
            self.chunk_controller.project_view().current_chunk_id,
            merged.chunk_id,
        )
        expected_members = {
            choice.identity
            for choice in self.chunk_controller.segment_choices()
            if choice.chunk_id == merged.chunk_id and choice.attached
        }
        self.assertEqual(len(expected_members), 3)
        self.assertEqual(
            len({identity.document_id for identity in expected_members}),
            2,
        )
        edit_members = {
            identity
            for row in range(self.window.segment_list.count())
            if (
                identity := self._identity_value(
                    self.window.segment_list.item(row).data(
                        Qt.ItemDataRole.UserRole
                    )
                )
            )
            is not None
        }
        self.assertEqual(edit_members, expected_members)
        self.assertEqual(len(edit_members), 3)

        self.assertTrue(
            self.window.set_workspace_mode(WorkspaceMode.BROWSE, persist=False)
        )
        self._events()
        browse_members = {
            identity
            for row in range(self.window.browse_table.rowCount())
            if self.window.browse_table.item(row, 0) is not None
            and (
                identity := self._identity_value(
                    self.window.browse_table.item(row, 0).data(
                        Qt.ItemDataRole.UserRole
                    )
                )
            )
            is not None
        }
        self.assertEqual(browse_members, expected_members)
        self.assertEqual(len(browse_members), 3)
        self.assertTrue(
            self.window.set_workspace_mode(WorkspaceMode.EDIT, persist=False)
        )
        self._events()

        # ACTIVE adds only the v2 current-Chunk scope; navigation does not grant edit.
        self.assertEqual(
            [
                self.window.workspace_search_scope.itemText(index)
                for index in range(self.window.workspace_search_scope.count())
            ],
            ["当前章节", "当前分工", "搜索全部章节"],
        )
        self._select_scope(CollaborativeSearchScopeV2.CURRENT_CHUNK)
        current_chunk_report = self._search("needle")
        self.assertEqual(current_chunk_report.total, 2)
        self.assertTrue(
            all(hit.access.may_edit_target for hit in current_chunk_report.hits)
        )
        self.assertFalse(self.window.target_editor.isReadOnly())

        outside_choice = next(
            choice
            for choice in self.chunk_controller.segment_choices()
            if choice.attached and choice.chunk_id != merged.chunk_id
        )
        outside_source = next(
            item.source
            for item in self.controller.workspace_view.segments
            if item.identity.document.document_id
            == outside_choice.identity.document_id
            and item.identity.local_segment_id
            == outside_choice.identity.local_segment_id
        )
        self._select_scope(CollaborativeSearchScopeV2.ENTIRE_PROJECT)
        entire_project_report = self._search(outside_source)
        self.assertEqual(entire_project_report.total, 1)
        self.assertFalse(entire_project_report.hits[0].access.may_edit_target)
        self.assertEqual(self.controller.current_segment.source, outside_source)
        self.assertTrue(self.window.target_editor.isReadOnly())
        self.assertFalse(self.window.confirm_button.isEnabled())

        # All write-shaped entry points remain denied outside the current Chunk.
        rejected_target = self.controller.current_segment.target
        with self.assertRaises(EditorControllerError):
            self.controller.update_workspace_target("must not publish")
        with self.assertRaises(EditorControllerError):
            self.controller.confirm_current()
        self.assertEqual(self.controller.current_segment.target, rejected_target)
        tm_card = self.window._tm_card(
            99,
            LegacyExactTMSuggestion(
                source=outside_source,
                target="TM must not publish",
                resource_id="tm-c4",
                resource_name="C4 TM",
            ),
        )
        term_card = self.window._term_card(
            99,
            TermSuggestion(
                source_term="outside-only",
                target_term="TERM must not publish",
                start_index=0,
                end_index=12,
                resource_id="terms-c4",
                resource_name="C4 terms",
            ),
        )
        self.assertFalse(tm_card.findChild(QPushButton, "applyTm_99").isEnabled())
        self.assertFalse(
            term_card.findChild(QPushButton, "insertTerm_99").isEnabled()
        )

        # A same-identity head remains undoable.  Rename the merged Chunk, then
        # undo that rename without resurrecting any IDs retired by split/merge.
        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "rename")
            dialog.rename_chunk.setCurrentIndex(
                dialog.rename_chunk.findData(merged.chunk_id)
            )
            dialog.rename_name.setText("Cross document renamed")
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)
        renamed = self.chunk_controller.project_view()
        self.assertIn(
            "Cross document renamed",
            {chunk.name for chunk in renamed.chunks},
        )

        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "undo")
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)
        restored = self.chunk_controller.project_view()
        self.assertEqual(
            {chunk.name: chunk.member_count for chunk in restored.chunks},
            {"Cross document": 3, "Alpha back": 1},
        )
        self.assertEqual({chunk.chunk_id for chunk in restored.chunks}, {
            merged.chunk_id,
            next(
                chunk.chunk_id
                for chunk in split_view.chunks
                if chunk.name == "Alpha back"
            ),
        })

        # Chunk metadata is sidecar state: ProjectPackage bytes stay untouched.
        self.assertEqual(self.package_path.read_bytes(), self.package_bytes)

        cold_repository = ResourceRepository(self.app_data)
        cold_controller, cold_composition = _compose_editor_controller(
            cold_repository
        )
        self._enable_matcher(cold_composition)
        cold_chunks = _compose_chunk_controller(cold_controller, cold_repository)
        cold_window = QtEditorWindow(
            cold_controller,
            chunk_controller=cold_chunks,
        )
        cold_window._show_error = lambda *_args: None
        cold_window.show()
        self._events()
        try:
            self.assertTrue(
                cold_window.open_project_package_path(self.package_path)
            )
            self._events()
            reopened = cold_chunks.project_view()
            self.assertIs(reopened.mode, ChunkApplicationMode.ACTIVE)
            self.assertEqual(
                {chunk.name: chunk.member_count for chunk in reopened.chunks},
                {"Cross document": 3, "Alpha back": 1},
            )
            self.assertTrue(
                all(
                    chunk.assigned_to_current_reference
                    for chunk in reopened.chunks
                )
            )
            self.assertIsNone(reopened.current_chunk_id)
            self.assertTrue(cold_window.target_editor.isReadOnly())
            self.assertEqual(cold_window.workspace_search_scope.count(), 3)
            self.assertEqual(self.package_path.read_bytes(), self.package_bytes)
        finally:
            cold_window._confirm_unsaved = lambda: True
            cold_window.close()
            cold_window.deleteLater()
            self._events()

    def test_real_manager_shift_selects_all_four_and_merges_with_default_name(self) -> None:
        dialog = self._manager()
        try:
            self._set_group_names(
                dialog.partition_group_count,
                dialog.partition_group_names,
                ("分工 1", "分工 2", "分工 3", "分工 4"),
            )
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)

        dialog = self._manager()
        try:
            self._choose_manager_action(dialog, "merge")
            self.assertEqual(dialog.chunk_table.rowCount(), 4)
            viewport = dialog.chunk_table.viewport()
            first = dialog.chunk_table.visualItemRect(
                dialog.chunk_table.item(0, 0)
            ).center()
            last = dialog.chunk_table.visualItemRect(
                dialog.chunk_table.item(3, 0)
            ).center()
            QTest.mouseClick(
                viewport,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                first,
            )
            QTest.mouseClick(
                viewport,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier,
                last,
            )
            self._events()
            self.assertEqual(len(dialog._selected_chunk_ids()), 4)
            self.assertEqual(dialog.merge_name.text(), "")
            self._publish_manager(dialog)
        finally:
            self._close_manager(dialog)

        merged = self.chunk_controller.project_view()
        self.assertEqual(
            tuple((chunk.name, chunk.member_count) for chunk in merged.chunks),
            (("合并分工", 4),),
        )

    def test_qt_chunk_surfaces_keep_store_domain_and_package_authority_out(self) -> None:
        forbidden_modules = {
            "collaborative_chunk_store",
            "collaborative_chunk_workspace_adapter",
            "collaborative_chunks",
            "project_package",
            "resource_package",
            "resource_repository",
            "tmx_context_interchange",
        }
        for filename in ("qt_editor_window.py", "qt_chunk_manager_dialog.py"):
            tree = ast.parse((_ROOT / filename).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported.add(node.module)
            violations = {
                module
                for module in imported
                if any(
                    module == forbidden or module.startswith(forbidden + ".")
                    for forbidden in forbidden_modules
                )
                or "provider" in module.lower()
            }
            self.assertEqual(violations, set(), filename)

    def test_installed_chunk_gate_keeps_legacy_json_and_txt_personally_editable(self) -> None:
        """The optional Workspace gate must be inert for both legacy lanes."""

        legacy_json = self.root / "legacy.json"
        legacy_json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "Legacy JSON",
                    "source_locale": "en",
                    "target_locale": "zh-CN",
                    "segments": [
                        {
                            "id": "legacy-1",
                            "source": "legacy json source",
                            "target": "",
                            "speaker": "",
                            "confirmed": False,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_txt = self.root / "legacy.txt"
        legacy_txt.write_text("legacy txt source\n", encoding="utf-8")
        expected_scopes = ["\u5f53\u524d\u7ae0\u8282", "\u641c\u7d22\u5168\u90e8\u7ae0\u8282"]

        self.window._confirm_unsaved = lambda: True
        for path, target in (
            (legacy_json, "JSON \u8bd1\u6587"),
            (legacy_txt, "TXT \u8bd1\u6587"),
        ):
            with self.subTest(suffix=path.suffix):
                self.assertTrue(self.window.open_project_path(path))
                self._events()
                self.assertFalse(self.controller.has_workspace)
                self.assertIsNone(self.window.findChild(QWidget, "chunkBar"))
                self.assertFalse(self.window.target_editor.isReadOnly())
                self.assertTrue(self.window.confirm_button.isEnabled())
                self.assertEqual(
                    [
                        self.window.workspace_search_scope.itemText(index)
                        for index in range(
                            self.window.workspace_search_scope.count()
                        )
                    ],
                    expected_scopes,
                )

                self.window.target_editor.setPlainText(target)
                self._events()
                self.assertEqual(self.controller.current_segment.target, target)

    def test_advanced_members_use_transient_browse_review_selection_session(self) -> None:
        first_member = self._plain_identity(0, "shared")
        self.chunk_controller.apply_mutation(
            self.chunk_controller.preview_create_chunk(
                "Initial scope",
                (first_member,),
            )
        )
        initial_view = self.chunk_controller.project_view()
        initial_chunk = initial_view.chunks[0]
        self.chunk_controller.select_current_chunk(initial_chunk.chunk_id)
        self.window._refresh_after_chunk_manager_change()
        self.assertTrue(
            self.window.set_workspace_mode(WorkspaceMode.EDIT, persist=False)
        )
        self.window.project_search_input.setText("selection sentinel")
        self._select_scope(CollaborativeSearchScopeV2.ENTIRE_PROJECT)
        self.window._set_project_search_expanded(True)
        self._events()
        previous_identity = self.controller.current_workspace_identity
        previous_chunk_id = self.chunk_controller.project_view().current_chunk_id
        previous_revision = self.chunk_controller.project_view().plan_revision

        self.window._open_chunk_manager()
        self._events()
        dialog = self.window._chunk_manager_dialog
        self.assertIsInstance(dialog, QtChunkManagerDialog)
        self.assertIs(
            dialog.windowModality(),
            Qt.WindowModality.WindowModal,
        )
        self._choose_manager_action(dialog, "create")
        dialog.create_name.setText("Selected in context")
        QTest.mouseClick(
            dialog.segment_selection_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()

        self.assertFalse(dialog.isVisible())
        self.assertTrue(self.window.chunk_segment_selection_bar.isVisible())
        self.assertIs(self.window.workspace_mode, WorkspaceMode.BROWSE)
        self.assertFalse(self.window.workspace_mode_combo.isEnabled())
        table_identities = {
            identity
            for row in range(self.window.browse_table.rowCount())
            if (
                identity := self._identity_value(
                    self.window.browse_table.item(row, 0).data(
                        Qt.ItemDataRole.UserRole
                    )
                    if self.window.browse_table.item(row, 0) is not None
                    else None
                )
            )
            is not None
        }
        self.assertEqual(len(table_identities), 4)
        allowed_rows = [
            row
            for row in range(self.window.browse_table.rowCount())
            if self.window._chunk_segment_identity_for_row(row) is not None
        ]
        self.assertEqual(len(allowed_rows), 3)
        self.assertEqual(self.window._selected_chunk_segment_identities(), ())
        _document_name, group_entries = self.window._browse_document_projection()
        allowed_keys = set(self.window._chunk_segment_allowed_map())
        self.assertTrue(group_entries)
        self.assertTrue(
            all(
                self.window._chunk_segment_identity_key(entry[3]) in allowed_keys
                for entry in group_entries
            )
        )
        group_target_row = allowed_rows[0]
        group_target = self.window.browse_table.item(
            group_target_row,
            0,
        ).data(Qt.ItemDataRole.UserRole)
        self.window.browse_group_turn_bar.set_previews(
            (
                BrowseGroupPreview(
                    ordinal=1,
                    total_groups=1,
                    start_index=0,
                    end_index=1,
                    source="Selection group",
                    target="",
                    issued_identity=group_target,
                ),
            ),
            document_name="Selection scope",
        )
        self.window.browse_group_turn_bar.ticks[0].click()
        self._events()
        self.assertEqual(self.window.browse_table.currentRow(), group_target_row)
        self.assertIs(self.controller.current_workspace_identity, previous_identity)
        self.window.browse_table.setCurrentCell(allowed_rows[-1], 1)
        self._events()
        self.assertIs(self.controller.current_workspace_identity, previous_identity)
        self.assertEqual(
            self.chunk_controller.project_view().plan_revision,
            previous_revision,
        )

        QTest.mouseClick(
            self.window.chunk_segment_bulk_select,
            Qt.MouseButton.LeftButton,
        )
        self.assertEqual(
            len(self.window._selected_chunk_segment_identities()),
            3,
        )
        QTest.mouseClick(
            self.window.chunk_segment_cancel,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertTrue(dialog.isVisible())
        self.assertIs(self.window.workspace_mode, WorkspaceMode.EDIT)
        self.assertIs(self.controller.current_workspace_identity, previous_identity)
        self.assertEqual(
            self.chunk_controller.project_view().current_chunk_id,
            previous_chunk_id,
        )
        self.assertEqual(
            self.window.project_search_input.text(),
            "selection sentinel",
        )
        self.assertTrue(self.window.project_search_panel.isVisible())
        self.assertEqual(dialog._selected_segments(), ())

        QTest.mouseClick(
            dialog.segment_selection_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        allowed_rows = [
            row
            for row in range(self.window.browse_table.rowCount())
            if self.window._chunk_segment_identity_for_row(row) is not None
        ]
        self.window.browse_table.setCurrentCell(allowed_rows[0], 0)
        QTest.mouseClick(
            self.window.chunk_segment_range_start,
            Qt.MouseButton.LeftButton,
        )
        self.window.browse_table.setCurrentCell(allowed_rows[-1], 0)
        QTest.mouseClick(
            self.window.chunk_segment_range_end,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        selected = self.window._selected_chunk_segment_identities()
        self.assertEqual(len(selected), 3)
        QTest.mouseClick(
            self.window.chunk_segment_done,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog._selected_segments(), selected)
        self.assertIs(self.window.workspace_mode, WorkspaceMode.EDIT)
        self.assertIs(self.controller.current_workspace_identity, previous_identity)
        self.assertEqual(
            self.chunk_controller.project_view().current_chunk_id,
            previous_chunk_id,
        )
        self.assertEqual(
            self.chunk_controller.project_view().plan_revision,
            previous_revision,
        )
        self._publish_manager(dialog)
        self.assertEqual(
            self.chunk_controller.project_view().plan_revision,
            previous_revision + 1,
        )
        self.assertIn(
            "Selected in context",
            tuple(
                chunk.name
                for chunk in self.chunk_controller.project_view().chunks
            ),
        )
        dialog.close()
        self._events()

    def test_browse_selection_session_has_no_topology_preview_or_apply_call(self) -> None:
        tree = ast.parse(
            (_ROOT / "qt_editor_window.py").read_text(encoding="utf-8")
        )
        selection_methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (
                "chunk_segment" in node.name
                or node.name == "_begin_chunk_segment_selection"
                or node.name == "_finish_chunk_segment_selection"
            )
        }
        self.assertIn("_begin_chunk_segment_selection", selection_methods)
        self.assertIn("_finish_chunk_segment_selection", selection_methods)
        called_attributes = {
            node.func.attr
            for method in selection_methods.values()
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(
            any(name.startswith("preview_") for name in called_attributes),
            called_attributes,
        )
        self.assertNotIn("apply_mutation", called_attributes)

    def test_entrypoint_injects_chunks_before_first_render_without_late_install(self) -> None:
        tree = ast.parse((_ROOT / "qt_editor.py").read_text(encoding="utf-8"))
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        window_calls = [
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "QtEditorWindow"
        ]
        self.assertEqual(len(window_calls), 1)
        self.assertIn(
            "chunk_controller",
            {keyword.arg for keyword in window_calls[0].keywords},
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Attribute)
                and node.attr == "install_chunk_controller"
                for node in ast.walk(main)
            )
        )

    def test_one_project_render_captures_chunk_view_and_membership_once(self) -> None:
        identity = self._plain_identity(0, "shared")
        self.chunk_controller.apply_mutation(
            self.chunk_controller.preview_create_chunk("Render scope", (identity,))
        )
        chunk_id = self.chunk_controller.project_view().chunks[0].chunk_id
        self.chunk_controller.select_current_chunk(chunk_id)
        with (
            patch.object(
                self.chunk_controller,
                "project_view",
                wraps=self.chunk_controller.project_view,
            ) as project_view,
            patch.object(
                self.chunk_controller,
                "segment_choices",
                wraps=self.chunk_controller.segment_choices,
            ) as segment_choices,
        ):
            self.window._render_project()
        self.assertEqual(project_view.call_count, 1)
        self.assertEqual(segment_choices.call_count, 1)

    def test_project_menu_uses_captured_chunk_view_without_owner_refresh(self) -> None:
        with patch.object(
            self.chunk_controller,
            "project_view",
            wraps=self.chunk_controller.project_view,
        ) as project_view:
            self.window.project_menu.aboutToShow.emit()
            self._events()
        self.assertEqual(project_view.call_count, 0)
        self.assertGreater(self.window.chunk_scope_menu.actions().__len__(), 0)

    def test_reconciliation_blocks_qt_until_previewed_rebase_is_published(self) -> None:
        members = (
            self._plain_identity(0, "shared"),
            self._plain_identity(1, "shared"),
        )
        self.chunk_controller.apply_mutation(
            self.chunk_controller.preview_create_chunk("Cross", members)
        )
        staged = self.controller.stage_workspace_source_rebind(
            self.sources,
            (self.first_source, self.second_source),
        )
        preview = self.controller.preview_workspace_reconciliation(staged)
        self.controller.apply_workspace_reconciliation(preview, staged)
        self.assertIs(
            self.chunk_controller.project_view().mode,
            ChunkApplicationMode.ACTIVE,
        )

        _write_source(
            self.second_source,
            name="Beta document",
            rows=(
                ("shared", "needle beta member"),
                ("tail", "second chunk member"),
                ("new", "new unallocated member"),
            ),
        )
        incoming = revalidate_staged_selected_documents(staged)
        preview = self.controller.preview_workspace_reconciliation(incoming)
        self.controller.apply_workspace_reconciliation(preview, incoming)
        self.window._render_current_segment()
        blocked = self.chunk_controller.project_view()
        self.assertIs(blocked.mode, ChunkApplicationMode.BLOCKED)
        self.assertEqual(blocked.safe_code, "CHUNK.REBASE_REQUIRED")
        self.assertTrue(self.window.target_editor.isReadOnly())
        self.assertTrue(self.window.chunk_manage_action.isEnabled())
        with self.assertRaisesRegex(
            EditorControllerError,
            "^CHUNK\\.REBASE_REQUIRED$",
        ):
            self.controller.update_workspace_target("must not publish")

        dialog = QtChunkManagerDialog(
            self.chunk_controller,
            blocked,
            self.window,
        )
        dialog.show()
        self._events()
        try:
            self.assertFalse(dialog.action_combo.isEnabled())
            self.assertEqual(dialog.action_combo.currentData(), "rebase")
            self.assertTrue(dialog.preview_button.isEnabled())
            QTest.mouseClick(
                dialog.preview_button,
                Qt.MouseButton.LeftButton,
            )
            self.assertTrue(dialog.confirm_check.isEnabled())
            dialog.confirm_check.setChecked(True)
            QTest.mouseClick(dialog.apply_button, Qt.MouseButton.LeftButton)
            self._events()
            self.assertIn("已发布", dialog.preview_panel.text())
        finally:
            dialog.close()
            dialog.deleteLater()
            self._events()
        active = self.chunk_controller.project_view()
        self.assertIs(active.mode, ChunkApplicationMode.ACTIVE)
        self.assertEqual(active.chunks[0].member_count, 2)
        self.assertEqual(active.unallocated_count, 3)


if __name__ == "__main__":
    unittest.main()
