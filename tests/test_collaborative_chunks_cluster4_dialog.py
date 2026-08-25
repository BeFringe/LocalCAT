"""Offscreen acceptance for the body-safe C4 Chunk manager dialog."""

from __future__ import annotations

import ast
from dataclasses import replace
import os
from pathlib import Path
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QSizePolicy

from chunk_controller_contracts import (
    ChunkApplicationAccessView,
    ChunkApplicationChunkView,
    ChunkApplicationMode,
    ChunkApplicationMutationPreview,
    ChunkApplicationMutationReceipt,
    ChunkApplicationProgressView,
    ChunkApplicationProjectView,
    ChunkApplicationRebaseInspection,
    ChunkApplicationSegmentChoice,
    ChunkApplicationSegmentSelectionRequest,
    ChunkApplicationSplitChild,
)
from project_workspace_contracts import SegmentIdentity
from qt_chunk_manager_dialog import QtChunkManagerDialog


_ROOT = Path(__file__).resolve().parents[1]
_DOC_A = "doc-" + "01" * 32


def _view() -> ChunkApplicationProjectView:
    identity = SegmentIdentity(_DOC_A, "seg-1")
    progress = ChunkApplicationProgressView(
        attached_total=2,
        unfilled=1,
        draft=0,
        confirmed=1,
        detached=0,
    )
    return ChunkApplicationProjectView(
        mode=ChunkApplicationMode.ACTIVE,
        project_id="project-c4-dialog",
        chunk_plan_id="plan-a",
        plan_revision=4,
        current_chunk_id="chunk-a",
        reference_label="本机工作流身份（非账号）",
        chunks=(
            ChunkApplicationChunkView(
                chunk_id="chunk-a",
                name="第一批",
                order=0,
                assignee_label="本机当前工作流身份（非账号）",
                assigned_to_current_reference=True,
                member_count=2,
                progress=progress,
                is_current=True,
            ),
            ChunkApplicationChunkView(
                chunk_id="chunk-b",
                name="第二批",
                order=1,
                assignee_label="未分配",
                assigned_to_current_reference=False,
                member_count=2,
                progress=progress,
                is_current=False,
            ),
        ),
        unallocated_count=1,
        current_segment_access=ChunkApplicationAccessView(
            identity=identity,
            access="editable_assigned",
            may_edit_target=True,
            may_change_confirmed=True,
            safe_codes=(),
        ),
        safe_code=None,
    )


def _no_plan_view() -> ChunkApplicationProjectView:
    return replace(
        _view(),
        mode=ChunkApplicationMode.NO_PLAN,
        chunk_plan_id=None,
        plan_revision=None,
        current_chunk_id=None,
        chunks=(),
        unallocated_count=5,
    )


class _SafeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Facade:
    def __init__(self, view: ChunkApplicationProjectView) -> None:
        self.view = view
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.applied: list[ChunkApplicationMutationPreview] = []
        self.blockers: tuple[str, ...] = ()
        self.apply_failure: str | None = None
        self.rebase_inspection = ChunkApplicationRebaseInspection((), 0, (), False)

    def segment_choices(self) -> tuple[ChunkApplicationSegmentChoice, ...]:
        membership = {
            "seg-1": ("chunk-a", "第一批"),
            "seg-2": ("chunk-a", "第一批"),
            "seg-3": ("chunk-b", "第二批"),
            "seg-4": ("chunk-b", "第二批"),
        }
        if self.view.mode is ChunkApplicationMode.NO_PLAN:
            membership = {}
        return tuple(
            ChunkApplicationSegmentChoice(
                identity=SegmentIdentity(_DOC_A, f"seg-{index}"),
                document_label="卷二",
                segment_label=f"{index:03d}",
                chunk_id=membership.get(f"seg-{index}", (None, None))[0],
                chunk_label=membership.get(f"seg-{index}", (None, None))[1],
                attached=index != 3,
            )
            for index in range(1, 6)
        )

    def inspect_workspace_rebase(self) -> ChunkApplicationRebaseInspection:
        return self.rebase_inspection

    def _preview(
        self, method: str, args: tuple[object, ...], kwargs: dict[str, object]
    ) -> ChunkApplicationMutationPreview:
        self.calls.append((method, args, kwargs))
        return ChunkApplicationMutationPreview(
            operation_id=f"operation-{len(self.calls)}",
            action=method.removeprefix("preview_"),
            project_id=self.view.project_id,
            chunk_plan_id=self.view.chunk_plan_id,
            base_revision=self.view.plan_revision,
            published_revision=(self.view.plan_revision or 0) + 1,
            affected_chunk_ids=("chunk-a",),
            created_chunk_ids=("chunk-new",),
            retired_chunk_ids=(),
            affected_chunk_count=2,
            created_chunk_count=1,
            retired_chunk_count=0,
            affected_member_count=3,
            assignment_count=1,
            missing_member_count=1,
            new_unallocated_count=2,
            warnings=("CHUNK.SAFE_WARNING",),
            blockers=self.blockers,
            truncated=False,
            classification="workspace_rebase" if method.endswith("rebase") else None,
        )

    def preview_create_chunk(self, *args: object, **kwargs: object):
        return self._preview("preview_create_chunk", args, kwargs)

    def preview_rename_chunk(self, *args: object, **kwargs: object):
        return self._preview("preview_rename_chunk", args, kwargs)

    def preview_merge_chunks(self, *args: object, **kwargs: object):
        return self._preview("preview_merge_chunks", args, kwargs)

    def preview_workspace_rebase(self, *args: object, **kwargs: object):
        return self._preview("preview_workspace_rebase", args, kwargs)

    def preview_undo_current_head(self, *args: object, **kwargs: object):
        return self._preview("preview_undo_current_head", args, kwargs)

    def __getattr__(self, name: str):
        if name.startswith("preview_"):
            return lambda *args, **kwargs: self._preview(name, args, kwargs)
        raise AttributeError(name)

    def apply_mutation(
        self, preview: ChunkApplicationMutationPreview
    ) -> ChunkApplicationMutationReceipt:
        if self.apply_failure is not None:
            raise _SafeFailure(self.apply_failure)
        self.applied.append(preview)
        return ChunkApplicationMutationReceipt(
            operation_id=preview.operation_id,
            action=preview.action,
            project_id=preview.project_id,
            chunk_plan_id=preview.chunk_plan_id,
            published_revision=preview.published_revision,
            affected_chunk_count=preview.affected_chunk_count,
            affected_member_count=preview.affected_member_count,
            assignment_count=preview.assignment_count,
            safe_issues=(),
        )

    def project_view(self) -> ChunkApplicationProjectView:
        return self.view


class Cluster4ChunkManagerDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def setUp(self) -> None:
        self.view = _view()
        self.facade = _Facade(self.view)
        self.dialog = QtChunkManagerDialog(self.facade, self.view)
        self.dialog.show()
        self._events()

    def tearDown(self) -> None:
        self.dialog.close()
        self.dialog.deleteLater()
        self._events()

    def _choose_primary(self, key: str) -> None:
        index = self.dialog.action_combo.findData(key)
        self.assertGreaterEqual(index, 0)
        self.dialog.action_combo.setCurrentIndex(index)
        self._events()

    def _choose_advanced(self, key: str) -> None:
        self.dialog.advanced_button.setChecked(True)
        index = self.dialog.advanced_combo.findData(key)
        self.assertGreaterEqual(index, 0)
        self.dialog.advanced_combo.setCurrentIndex(index)
        self._events()

    def _select_chunk_rows(self, *rows: int) -> None:
        self.dialog.chunk_table.clearSelection()
        model = self.dialog.chunk_table.selectionModel()
        for row in rows:
            model.select(
                self.dialog.chunk_table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        self._events()

    def _preview(self) -> None:
        QTest.mouseClick(self.dialog.preview_button, Qt.MouseButton.LeftButton)
        self._events()

    def test_qt_module_imports_only_frozen_chunk_application_contracts(self) -> None:
        tree = ast.parse(
            (_ROOT / "qt_chunk_manager_dialog.py").read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        chunk_imports = {
            module
            for module in imports
            if "chunk" in module or "package" in module or "resource" in module
        }
        self.assertEqual(chunk_imports, {"chunk_controller_contracts"})
        source = (_ROOT / "qt_chunk_manager_dialog.py").read_text(encoding="utf-8")
        for forbidden in (
            "collaborative_chunk_store",
            "collaborative_chunks",
            "collaborative_chunk_workspace_adapter",
            "project_package",
            "resource_package",
            "tmx",
        ):
            self.assertNotIn(f"from {forbidden}", source)
            self.assertNotIn(f"import {forbidden}", source)

    def test_no_plan_partitions_project_directly_into_dynamic_groups(self) -> None:
        self.dialog.close()
        self.dialog.deleteLater()
        self.view = _no_plan_view()
        self.facade = _Facade(self.view)
        self.dialog = QtChunkManagerDialog(self.facade, self.view)
        self.dialog.show()
        self._events()

        self.assertEqual(self.dialog.action_combo.count(), 1)
        self.assertEqual(self.dialog.action_combo.currentData(), "partition")
        self.assertEqual(self.dialog.chunk_table.item(0, 0).text(), "整个项目")
        self.assertFalse(self.dialog.segment_selection_panel.isVisible())
        self.assertFalse(hasattr(self.dialog, "segment_table"))
        self.dialog.partition_group_count.setValue(3)
        self.assertEqual(self.dialog.partition_group_names.count(), 3)
        for index, name in enumerate(("前部", "中部", "后部")):
            self.dialog.partition_group_names.item(index).setText(name)
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            ("preview_partition_project", (("前部", "中部", "后部"),), {}),
        )
        self.assertNotIn("MEMBERS_REQUIRED", self.dialog.preview_panel.text())

    def test_assigned_chunk_split_is_dynamic_explicit_and_needs_no_segment_selection(self) -> None:
        self.assertEqual(self.dialog.action_combo.currentData(), "split_evenly")
        self.assertEqual(self.dialog._selected_segments(), ())
        self._select_chunk_rows(0)
        self.dialog.split_group_count.setValue(2)
        self.dialog.split_group_names.item(0).setText("前半")
        self.dialog.split_group_names.item(1).setText("后半")

        self._preview()
        self.assertIn("CHUNK.ASSIGNMENT_DECISION_REQUIRED", self.dialog.preview_panel.text())
        self.dialog.split_assignment.setCurrentIndex(
            self.dialog.split_assignment.findData("inherit")
        )
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            ("preview_split_chunk_evenly", ("chunk-a", ("前半", "后半"), "inherit"), {}),
        )
        self.assertIn("影响", self.dialog.preview_panel.text())
        self.assertIn("CHUNK.SAFE_WARNING", self.dialog.preview_panel.text())
        self.assertNotIn("seg-1", self.dialog.preview_panel.text())
        self.assertTrue(self.dialog.confirm_check.isEnabled())
        self.assertFalse(self.dialog.apply_button.isEnabled())

        self.dialog.confirm_check.setChecked(True)
        self.assertTrue(self.dialog.apply_button.isEnabled())
        QTest.mouseClick(self.dialog.apply_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.dialog.apply_button, Qt.MouseButton.LeftButton)
        self._events()

        self.assertEqual(len(self.facade.applied), 1)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertIn("已发布", self.dialog.preview_panel.text())
        self.assertIn("操作：拆分分工", self.dialog.preview_panel.text())
        self.assertNotIn("操作：create", self.dialog.preview_panel.text())

    def test_merge_is_direct_multi_chunk_scope_and_blocker_stays_preview_only(self) -> None:
        self._choose_primary("merge")
        self._select_chunk_rows(0, 1)
        self.dialog.merge_name.setText("合并结果")
        self.dialog.merge_assignment.setCurrentIndex(1)
        self.facade.blockers = ("CHUNK.MEMBERS_OVERLAP",)
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            (
                "preview_merge_chunks",
                (("chunk-a", "chunk-b"), "合并结果"),
                {"assign_to_current_reference": True},
            ),
        )
        self.assertIn("CHUNK.MEMBERS_OVERLAP", self.dialog.preview_panel.text())
        self.assertFalse(self.dialog.confirm_check.isEnabled())
        self.dialog.confirm_check.setChecked(True)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertEqual(self.facade.applied, [])

    def test_merge_shift_range_and_select_all_reach_every_chunk(self) -> None:
        first, second = self.view.chunks
        third = replace(
            first,
            chunk_id="chunk-c",
            name="第三批",
            order=2,
            is_current=False,
        )
        fourth = replace(
            second,
            chunk_id="chunk-d",
            name="第四批",
            order=3,
        )
        four_chunk_view = replace(
            self.view,
            chunks=(first, second, third, fourth),
        )
        self.dialog.refresh(four_chunk_view)
        self._choose_primary("merge")
        viewport = self.dialog.chunk_table.viewport()
        first_position = self.dialog.chunk_table.visualItemRect(
            self.dialog.chunk_table.item(0, 0)
        ).center()
        last_position = self.dialog.chunk_table.visualItemRect(
            self.dialog.chunk_table.item(3, 0)
        ).center()
        QTest.mouseClick(
            viewport,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            first_position,
        )
        QTest.mouseClick(
            viewport,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            last_position,
        )
        self._events()
        self.assertEqual(
            self.dialog._selected_chunk_ids(),
            ("chunk-a", "chunk-b", "chunk-c", "chunk-d"),
        )
        self.assertIn("已选 4 / 4", self.dialog.chunk_scope_hint.text())

        self.dialog.chunk_table.clearSelection()
        QTest.mouseClick(
            self.dialog.select_all_chunks_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(len(self.dialog._selected_chunk_ids()), 4)

    def test_merge_empty_name_uses_a_stable_default(self) -> None:
        self._choose_primary("merge")
        self._select_chunk_rows(0, 1)
        self.assertEqual(self.dialog.merge_name.text(), "")
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            (
                "preview_merge_chunks",
                (("chunk-a", "chunk-b"), None),
                {"assign_to_current_reference": None},
            ),
        )
        self.assertTrue(self.dialog.confirm_check.isEnabled())

    def test_changed_inputs_make_the_old_preview_visibly_stale(self) -> None:
        self._choose_primary("merge")
        self._select_chunk_rows(0, 1)
        self.dialog.merge_name.setText("初始结果")
        self._preview()
        self.assertTrue(self.dialog.confirm_check.isEnabled())
        self.dialog.merge_name.setText("变更后的结果")
        self._events()
        self.assertFalse(self.dialog.confirm_check.isEnabled())
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertIn("设置已变化", self.dialog.preview_panel.text())
        self.assertEqual(self.dialog.preview_button.text(), "重新生成预览")

    def test_manager_requests_browse_review_selection_for_advanced_actions(self) -> None:
        self.assertFalse(self.dialog.segment_selection_panel.isVisible())
        self.assertFalse(hasattr(self.dialog, "segment_table"))
        self._choose_advanced("create")
        self.assertTrue(self.dialog.segment_selection_panel.isVisible())
        self.assertTrue(self.dialog.advanced_help.isVisible())
        self.assertIn("浏览 / 校对页", self.dialog.advanced_help.text())
        self.assertIn("浏览 / 校对", self.dialog.segment_selection_button.text())
        requests: list[ChunkApplicationSegmentSelectionRequest] = []
        self.dialog.segmentSelectionRequested.connect(requests.append)
        QTest.mouseClick(
            self.dialog.segment_selection_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.action, "create")
        self.assertEqual(
            request.allowed_identities,
            (SegmentIdentity(_DOC_A, "seg-5"),),
        )
        self.assertEqual(request.bulk_select_identities, request.allowed_identities)
        self.assertIn("尚未分工", request.bulk_select_label)
        with self.assertRaisesRegex(
            ValueError,
            "CHUNK.SEGMENT_SELECTION_INVALID",
        ):
            self.dialog.accept_segment_selection(
                request,
                (SegmentIdentity(_DOC_A, "seg-1"),),
            )
        self.dialog.accept_segment_selection(
            request,
            (SegmentIdentity(_DOC_A, "seg-5"),),
        )
        with self.assertRaisesRegex(
            ValueError,
            "CHUNK.SEGMENT_SELECTION_STALE",
        ):
            self.dialog.accept_segment_selection(
                request,
                (SegmentIdentity(_DOC_A, "seg-5"),),
            )
        self.dialog.create_name.setText("未分配收尾")
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            (
                "preview_create_chunk",
                ("未分配收尾", (SegmentIdentity(_DOC_A, "seg-5"),)),
                {},
            ),
        )
        self.assertIn("已选择 1 个段落", self.dialog.selection_summary.text())

    def test_status_and_operation_controls_remain_compact_and_legible(self) -> None:
        self.assertIn("已启用", self.dialog.mode_badge.text())
        self.assertIn("2 个分工", self.dialog.mode_badge.text())
        self.assertLessEqual(self.dialog.mode_badge.maximumHeight(), 36)
        self.assertEqual(
            self.dialog.mode_badge.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Fixed,
        )
        self.assertEqual(self.dialog.chunk_table.item(0, 0).text(), "第一批")
        self.assertIn("当前分工", self.dialog.chunk_table.item(0, 0).toolTip())
        self.assertEqual(
            self.dialog.chunk_table.horizontalHeader().sectionResizeMode(3),
            QHeaderView.ResizeMode.Stretch,
        )
        self.assertGreaterEqual(self.dialog.action_combo.minimumWidth(), 170)
        self.assertGreaterEqual(self.dialog.advanced_combo.minimumWidth(), 280)
        primary_height = self.dialog.operation_pages.sizeHint().height()
        self._choose_advanced("create")
        create_height = self.dialog.operation_pages.sizeHint().height()
        self.assertEqual(
            create_height,
            self.dialog.operation_pages.currentWidget().sizeHint().height(),
        )
        self.assertLess(create_height, 180)
        self.assertLess(primary_height, 240)

    def test_advanced_request_is_exact_source_scope_in_project_order(self) -> None:
        self._choose_advanced("move")
        self.assertTrue(self.dialog.segment_selection_panel.isVisible())
        requests: list[ChunkApplicationSegmentSelectionRequest] = []
        self.dialog.segmentSelectionRequested.connect(requests.append)
        QTest.mouseClick(
            self.dialog.segment_selection_button,
            Qt.MouseButton.LeftButton,
        )
        self._events()
        self.assertEqual(
            requests[0].allowed_identities,
            (
                SegmentIdentity(_DOC_A, "seg-1"),
                SegmentIdentity(_DOC_A, "seg-2"),
            ),
        )
        self.assertIsNone(requests[0].bulk_select_label)
        self.dialog.cancel_segment_selection(requests[0])

    def test_advanced_routes_and_exact_split_publish_assignment_decision(self) -> None:
        self._choose_advanced("rename")
        self.dialog.rename_name.setText("新名称")
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            ("preview_rename_chunk", ("chunk-a", "新名称"), {}),
        )

        first = SegmentIdentity(_DOC_A, "seg-1")
        second = SegmentIdentity(_DOC_A, "seg-2")
        self._choose_advanced("reorder")
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            ("preview_reorder_chunks", (("chunk-a", "chunk-b"),), {}),
        )

        self.dialog.set_split_children(
            (
                ChunkApplicationSplitChild("子分工一", (first,)),
                ChunkApplicationSplitChild("子分工二", (second,)),
            )
        )
        self._choose_advanced("exact_split")
        self._select_chunk_rows(0)
        self.dialog.exact_split_assignment.setCurrentIndex(
            self.dialog.exact_split_assignment.findData("inherit")
        )
        self._preview()
        self.assertEqual(self.facade.calls[-1][0], "preview_split_chunk")
        self.assertEqual(self.facade.calls[-1][1][0], "chunk-a")
        children = self.facade.calls[-1][1][1]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.assign_to_current_reference is True for child in children))

        self.dialog.set_selected_segment_identities((first,))
        self._choose_advanced("move")
        self.dialog.move_destination.setCurrentIndex(1)
        self._preview()
        self.assertEqual(self.facade.calls[-1][0], "preview_move_members")
        self.assertEqual(self.facade.calls[-1][1][:2], ("chunk-a", "chunk-b"))

        expected_methods = {
            "release": "preview_release_members",
            "dissolve_chunk": "preview_dissolve_chunk",
            "dissolve_plan": "preview_dissolve_plan",
            "assign": "preview_assign_to_current_reference",
            "reassign": "preview_reassign_to_current_reference",
            "unassign": "preview_unassign_chunk",
        }
        for action, expected in expected_methods.items():
            with self.subTest(action=action):
                self._choose_advanced(action)
                self._preview()
                self.assertEqual(self.facade.calls[-1][0], expected)

    def test_apply_failure_keeps_dialog_open_and_shows_only_safe_code(self) -> None:
        self.facade.apply_failure = "CHUNK.PREVIEW_STALE"
        self.dialog.split_assignment.setCurrentIndex(
            self.dialog.split_assignment.findData("inherit")
        )
        self._preview()
        self.dialog.confirm_check.setChecked(True)

        QTest.mouseClick(self.dialog.apply_button, Qt.MouseButton.LeftButton)
        self._events()

        self.assertTrue(self.dialog.isVisible())
        self.assertIn("CHUNK.PREVIEW_STALE", self.dialog.preview_panel.text())
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertEqual(self.facade.applied, [])

    def test_resizable_split_layout_and_primary_accessibility(self) -> None:
        self.assertTrue(self.dialog.isSizeGripEnabled())
        self.assertGreater(self.dialog.maximumWidth(), self.dialog.minimumWidth())
        self.dialog.resize(840, 580)
        self._events()
        sizes = self.dialog.body_splitter.sizes()
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(size > 0 for size in sizes))
        for widget in (
            self.dialog.action_combo,
            self.dialog.chunk_table,
            self.dialog.split_group_count,
            self.dialog.split_group_names,
            self.dialog.split_assignment,
            self.dialog.preview_button,
            self.dialog.apply_button,
        ):
            self.assertTrue(widget.accessibleName())
            self.assertNotEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_rebase_requires_explicit_decisions_and_all_empty_can_dissolve(self) -> None:
        self.dialog.close()
        self.dialog.deleteLater()
        missing = SegmentIdentity(_DOC_A, "seg-1")
        self.view = replace(
            _view(),
            mode=ChunkApplicationMode.BLOCKED,
            safe_code="CHUNK.REBASE_REQUIRED",
        )
        self.facade = _Facade(self.view)
        self.facade.rebase_inspection = ChunkApplicationRebaseInspection(
            (missing,), 2, ("chunk-b",), False
        )
        self.dialog = QtChunkManagerDialog(self.facade, self.view)
        self.dialog.show()
        self._events()

        self.assertEqual(self.dialog.action_combo.currentData(), "rebase")
        self._preview()
        self.assertIn("CHUNK.REBASE_DECISION_REQUIRED", self.dialog.preview_panel.text())
        self.dialog.rebase_missing_decision.setCurrentIndex(
            self.dialog.rebase_missing_decision.findData("release")
        )
        self.dialog.rebase_empty_decision.setCurrentIndex(
            self.dialog.rebase_empty_decision.findData("keep")
        )
        self._preview()
        self.assertEqual(
            self.facade.calls[-1],
            ("preview_workspace_rebase", ((missing,), ()), {}),
        )

        self.facade.rebase_inspection = ChunkApplicationRebaseInspection(
            (missing,), 0, ("chunk-a", "chunk-b"), True
        )
        self.dialog.refresh(self.view)
        self.dialog.rebase_missing_decision.setCurrentIndex(
            self.dialog.rebase_missing_decision.findData("release")
        )
        self.dialog.rebase_empty_decision.setCurrentIndex(
            self.dialog.rebase_empty_decision.findData("dissolve")
        )
        self._preview()
        self.assertEqual(self.facade.calls[-1][0], "preview_dissolve_plan")

    def test_all_buttons_are_localized(self) -> None:
        labels = {
            button.text()
            for button in self.dialog.findChildren(type(self.dialog.close_button))
        }
        self.assertNotIn("Cancel", labels)
        self.assertNotIn("Apply", labels)
        self.assertIn("关闭", labels)
        self.assertIn("确认发布", labels)


if __name__ == "__main__":
    unittest.main()
