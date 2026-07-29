from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from editor_contracts import (
    BatchOperationReport,
    BatchUndoState,
    DEFAULT_EDITOR_FONT_SIZE,
    LiteralReplaceRule,
    MAX_EDITOR_FONT_SIZE,
    MIN_EDITOR_FONT_SIZE,
    ConfirmResult,
    DisplayPreferences,
    EditorProject,
    EditorSegment,
    ImportReport,
    ImportRequest,
    PreprocessChange,
    PreprocessPreview,
    ResourceConfig,
    ResourceKind,
    SuggestionBundle,
    TMSuggestion,
    TermSuggestion,
    WriteReport,
    SegmentDensity,
    WorkspaceMode,
)


class EditorContractsTest(unittest.TestCase):
    def test_contracts_are_frozen_and_tuple_based(self) -> None:
        segment = EditorSegment(id="seg-1", source="Hello")
        project = EditorProject(name="Demo", segments=(segment,))

        with self.assertRaises(dataclasses.FrozenInstanceError):
            segment.target = "你好"  # type: ignore[misc]

        self.assertIsInstance(project.segments, tuple)
        self.assertEqual(project.segments[0].source, "Hello")

    def test_resource_and_suggestion_contracts_keep_provenance(self) -> None:
        resource = ResourceConfig(
            id="tm-main",
            name="Main TM",
            kind=ResourceKind.TRANSLATION_MEMORY,
            path=Path("/tmp/main.jsonl"),
        )
        tm = TMSuggestion(
            source="Hello",
            target="你好",
            resource_id=resource.id,
            resource_name=resource.name,
        )
        term = TermSuggestion(
            source_term="Hello",
            target_term="你好",
            start_index=0,
            end_index=5,
            resource_id="terms-main",
            resource_name="Main terms",
        )
        bundle = SuggestionBundle(tm_matches=(tm,), terms=(term,))

        self.assertEqual(bundle.tm_matches[0].similarity, 1.0)
        self.assertEqual(bundle.terms[0].resource_name, "Main terms")

    def test_structured_operation_reports(self) -> None:
        imported = ImportReport(imported=3, skipped=1, overwritten=1)
        write_report = WriteReport(written_resource_ids=("tm-main",))
        project = EditorProject(
            name="Demo",
            segments=(EditorSegment(id="1", source="Hello", target="你好"),),
        )
        result = ConfirmResult(project=project, current_index=0, write_report=write_report)
        request = ImportRequest(
            resource_id="tm-main",
            input_path=Path("/tmp/demo.tmx"),
            source_locale="en-US",
            target_locale="zh-CN",
        )

        self.assertTrue(imported.succeeded)
        self.assertTrue(write_report.succeeded)
        self.assertEqual(result.project.segments[0].target, "你好")
        self.assertEqual(request.input_path.suffix, ".tmx")

    def test_invalid_contract_values_fail_fast(self) -> None:
        with self.assertRaises(ValueError):
            EditorSegment(id="", source="Hello")
        with self.assertRaises(ValueError):
            EditorSegment(id="1", source="")
        with self.assertRaises(ValueError):
            ResourceConfig(
                id="tm",
                name="Main",
                kind=ResourceKind.TRANSLATION_MEMORY,
                path=Path(),
            )
        with self.assertRaises(ValueError):
            ImportReport(imported=-1)
        with self.assertRaises(TypeError):
            EditorProject(name="Demo", segments=[])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TMSuggestion(
                source="Hello",
                target="你好",
                resource_id="tm",
                resource_name="Main",
                similarity=1.5,
            )

    def test_display_preferences_validate_and_preserve_editor_font_size(self) -> None:
        self.assertEqual(
            DisplayPreferences().editor_font_size,
            DEFAULT_EDITOR_FONT_SIZE,
        )
        for size in (MIN_EDITOR_FONT_SIZE, DEFAULT_EDITOR_FONT_SIZE, MAX_EDITOR_FONT_SIZE):
            self.assertEqual(DisplayPreferences(editor_font_size=size).editor_font_size, size)

        for invalid in (True, False, 15.0, "15"):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                DisplayPreferences(editor_font_size=invalid)  # type: ignore[arg-type]
        for invalid in (MIN_EDITOR_FONT_SIZE - 1, MAX_EDITOR_FONT_SIZE + 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                DisplayPreferences(editor_font_size=invalid)

        original = DisplayPreferences(
            segment_density=SegmentDensity.WRAPPED,
            workspace_mode=WorkspaceMode.BROWSE,
            editor_font_size=21,
        )
        resized = dataclasses.replace(original, editor_font_size=22)
        self.assertIs(resized.segment_density, SegmentDensity.WRAPPED)
        self.assertIs(resized.workspace_mode, WorkspaceMode.BROWSE)
        self.assertEqual(resized.editor_font_size, 22)

    def test_preprocess_contracts_are_frozen_and_preserve_ordered_changes(self) -> None:
        rules = (
            LiteralReplaceRule(find="colour", replacement="color", enabled=True),
            LiteralReplaceRule(find="  ", replacement=" ", enabled=False),
        )
        first_change = PreprocessChange(
            segment_id="seg-2",
            segment_index=1,
            before_target="旧译文",
            after_target="新译文",
            before_confirmed=True,
            after_confirmed=False,
        )
        second_change = PreprocessChange(
            segment_id="seg-7",
            segment_index=6,
            before_target="A  B",
            after_target="A B",
            before_confirmed=False,
            after_confirmed=False,
        )
        preview = PreprocessPreview(
            project_session_id="session-1",
            base_revision=8,
            changes=(first_change, second_change),
        )

        self.assertEqual(tuple(rule.find for rule in rules), ("colour", "  "))
        self.assertEqual(
            tuple(change.segment_id for change in preview.changes),
            ("seg-2", "seg-7"),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first_change.after_target = "其他译文"  # type: ignore[misc]

    def test_preview_and_undo_contracts_carry_stale_state_evidence(self) -> None:
        change = PreprocessChange(
            segment_id="seg-3",
            segment_index=2,
            before_target="Before",
            after_target="After",
            before_confirmed=True,
            after_confirmed=False,
        )
        preview = PreprocessPreview(
            project_session_id="session-a",
            base_revision=11,
            changes=(change,),
        )
        no_change_preview = PreprocessPreview(
            project_session_id="session-a",
            base_revision=11,
            changes=(),
        )
        undo = BatchUndoState(
            project_session_id="session-a",
            applied_revision=12,
            dirty_before=False,
            saved_baseline_digest_at_apply="baseline-sha256",
            changes=(change,),
        )

        self.assertEqual(preview.project_session_id, undo.project_session_id)
        self.assertEqual(preview.base_revision + 1, undo.applied_revision)
        self.assertEqual(
            (
                change.before_target,
                change.after_target,
                change.before_confirmed,
                change.after_confirmed,
            ),
            ("Before", "After", True, False),
        )
        self.assertEqual(no_change_preview.changes, ())
        self.assertEqual(undo.saved_baseline_digest_at_apply, "baseline-sha256")

    def test_batch_operation_report_expresses_changes_and_no_change(self) -> None:
        applied = BatchOperationReport(
            operation="apply",
            project_session_id="session-a",
            resulting_revision=12,
            changed_segment_ids=("seg-3", "seg-8"),
            dirty=True,
        )
        no_change = BatchOperationReport(
            operation="undo",
            project_session_id="session-a",
            resulting_revision=12,
            changed_segment_ids=(),
            dirty=False,
        )

        self.assertEqual(applied.changed_segment_ids, ("seg-3", "seg-8"))
        self.assertTrue(applied.dirty)
        self.assertEqual(no_change.changed_segment_ids, ())
        self.assertFalse(no_change.dirty)

    def test_preprocess_contracts_reject_incomplete_or_ambiguous_state(self) -> None:
        valid_change = PreprocessChange(
            segment_id="seg-1",
            segment_index=0,
            before_target="Before",
            after_target="After",
            before_confirmed=True,
            after_confirmed=False,
        )

        invalid_calls = (
            lambda: LiteralReplaceRule(find="", replacement="x", enabled=True),
            lambda: LiteralReplaceRule(find="x", replacement="y", enabled="yes"),  # type: ignore[arg-type]
            lambda: PreprocessChange(
                segment_id="",
                segment_index=0,
                before_target="Before",
                after_target="After",
                before_confirmed=True,
                after_confirmed=False,
            ),
            lambda: PreprocessChange(
                segment_id="seg-1",
                segment_index=-1,
                before_target="Before",
                after_target="After",
                before_confirmed=True,
                after_confirmed=False,
            ),
            lambda: PreprocessChange(
                segment_id="seg-1",
                segment_index=0,
                before_target="Same",
                after_target="Same",
                before_confirmed=True,
                after_confirmed=False,
            ),
            lambda: PreprocessChange(
                segment_id="seg-1",
                segment_index=0,
                before_target="Before",
                after_target="After",
                before_confirmed=True,
                after_confirmed=True,
            ),
            lambda: PreprocessPreview(
                project_session_id="",
                base_revision=1,
                changes=(valid_change,),
            ),
            lambda: PreprocessPreview(
                project_session_id="session-a",
                base_revision=-1,
                changes=(valid_change,),
            ),
            lambda: PreprocessPreview(
                project_session_id="session-a",
                base_revision=1,
                changes=[valid_change],  # type: ignore[arg-type]
            ),
            lambda: PreprocessPreview(
                project_session_id="session-a",
                base_revision=1,
                changes=(valid_change, valid_change),
            ),
            lambda: BatchOperationReport(
                operation="apply",
                project_session_id="session-a",
                resulting_revision=2,
                changed_segment_ids=("seg-1", "seg-1"),
                dirty=True,
            ),
            lambda: BatchUndoState(
                project_session_id="session-a",
                applied_revision=2,
                dirty_before=False,
                saved_baseline_digest_at_apply="baseline",
                changes=(),
            ),
            lambda: BatchUndoState(
                project_session_id="session-a",
                applied_revision=2,
                dirty_before=False,
                saved_baseline_digest_at_apply="",
                changes=(valid_change,),
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_call()


if __name__ == "__main__":
    unittest.main()
