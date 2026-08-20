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
    LegacyExactTMSuggestion,
    LegacyTermRow,
    PreprocessChange,
    PreprocessPreferences,
    PreprocessPreview,
    PreparedTermMutation,
    ResourceConfig,
    ResourceKind,
    SuggestionBundle,
    TermCleanupReport,
    TermCommitOutcome,
    TermCommitState,
    TermDraft,
    TermMatchPolicy,
    TermMutationReport,
    TermRecord,
    TermRecordLocator,
    TermRowKind,
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
        tm = LegacyExactTMSuggestion(
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
            LegacyExactTMSuggestion(
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

        preferences = PreprocessPreferences(
            rules=rules,
            include_draft=True,
            include_confirmed=False,
        )
        self.assertEqual(
            tuple(rule.find for rule in preferences.rules),
            ("colour", "  "),
        )
        self.assertTrue(preferences.include_draft)
        self.assertFalse(preferences.include_confirmed)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            preferences.include_confirmed = True  # type: ignore[misc]

    def test_preprocess_preferences_reject_ambiguous_or_foreign_state(self) -> None:
        rule = LiteralReplaceRule(find="x", replacement="y", enabled=True)
        invalid_calls = (
            lambda: PreprocessPreferences(
                include_draft=False,
                include_confirmed=False,
            ),
            lambda: PreprocessPreferences(rules=[rule]),  # type: ignore[arg-type]
            lambda: PreprocessPreferences(
                rules=(rule,),
                include_draft=1,  # type: ignore[arg-type]
            ),
            lambda: PreprocessPreferences(
                rules=(rule,),
                include_confirmed="yes",  # type: ignore[arg-type]
            ),
        )
        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_call()

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

    def test_mixed_termbase_contracts_are_frozen_and_preserve_row_identity(
        self,
    ) -> None:
        file_digest = "a" * 64
        legacy_locator = TermRecordLocator(
            row_kind=TermRowKind.LEGACY,
            file_digest=file_digest,
            row_ordinal=0,
            row_digest="b" * 64,
            record_id=None,
        )
        v1_locator = TermRecordLocator(
            row_kind=TermRowKind.V1,
            file_digest=file_digest,
            row_ordinal=1,
            row_digest="c" * 64,
            record_id="term-7",
        )
        legacy = TermRecord(
            locator=legacy_locator,
            record_id=None,
            source="legacy",
            target="旧术语",
            policy=TermMatchPolicy.LEGACY,
            match_case=None,
            whole_word=None,
        )
        configured = TermRecord(
            locator=v1_locator,
            record_id="term-7",
            source="Configured",
            target="新术语",
            policy=TermMatchPolicy.CONFIGURED,
            match_case=False,
            whole_word=True,
        )
        draft = TermDraft(source="New", target="新增")
        incoming = LegacyTermRow(source="Imported", target="导入", input_ordinal=3)
        prepared = PreparedTermMutation(
            action="update",
            resource_path=Path("/tmp/terms.csv"),
            base_digest=file_digest,
            staged_path=Path("/tmp/.terms.csv.stage"),
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            candidate_records=(legacy, configured),
        )

        self.assertEqual(draft.match_case, False)
        self.assertEqual(draft.whole_word, True)
        self.assertEqual(incoming.input_ordinal, 3)
        self.assertEqual(prepared.candidate_records, (legacy, configured))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            configured.target = "changed"  # type: ignore[misc]

    def test_term_commit_and_cleanup_contracts_express_recovery_states(self) -> None:
        record = TermRecord(
            locator=TermRecordLocator(
                row_kind=TermRowKind.V1,
                file_digest="a" * 64,
                row_ordinal=0,
                row_digest="b" * 64,
                record_id="term-1",
            ),
            record_id="term-1",
            source="source",
            target="target",
            policy=TermMatchPolicy.CONFIGURED,
            match_case=False,
            whole_word=True,
        )
        report = TermMutationReport(
            action="create",
            resource_path=Path("/tmp/terms.csv"),
            committed_digest="c" * 64,
            records=(record,),
            created=1,
            updated=0,
            deleted=0,
            imported=0,
            overwritten=0,
        )
        committed = TermCommitOutcome(
            state=TermCommitState.COMMITTED,
            report=report,
            error_code=None,
            retryable=False,
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            quarantined=False,
            safe_detail=None,
        )
        not_committed = TermCommitOutcome(
            state=TermCommitState.NOT_COMMITTED,
            report=None,
            error_code="SOURCE_CHANGED",
            retryable=True,
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            quarantined=False,
            safe_detail="Reload the termbase and retry.",
        )
        rolled_back = TermCommitOutcome(
            state=TermCommitState.ROLLED_BACK,
            report=None,
            error_code="DIRECTORY_FSYNC_FAILED",
            retryable=True,
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            quarantined=False,
            safe_detail="The previous bytes were restored; retry the change.",
        )
        indeterminate = TermCommitOutcome(
            state=TermCommitState.INDETERMINATE,
            report=None,
            error_code="ROLLBACK_FAILED",
            retryable=False,
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            quarantined=True,
            safe_detail="Restore the resource from the recovery file.",
        )
        cleaned = TermCleanupReport(
            cleaned=True,
            recovery_path=None,
            warning_code=None,
        )
        cleanup_warning = TermCleanupReport(
            cleaned=False,
            recovery_path=Path("/tmp/.terms.csv.recovery"),
            warning_code="RECOVERY_DELETE_FAILED",
        )

        self.assertIs(committed.report, report)
        self.assertTrue(not_committed.retryable)
        self.assertTrue(rolled_back.retryable)
        self.assertTrue(indeterminate.quarantined)
        self.assertTrue(cleaned.cleaned)
        self.assertFalse(cleanup_warning.cleaned)
        self.assertEqual(
            cleanup_warning.recovery_path,
            Path("/tmp/.terms.csv.recovery"),
        )

    def test_term_rows_reject_legacy_v1_identity_and_policy_mismatches(self) -> None:
        valid_legacy_locator = TermRecordLocator(
            row_kind=TermRowKind.LEGACY,
            file_digest="a" * 64,
            row_ordinal=0,
            row_digest="b" * 64,
            record_id=None,
        )
        valid_v1_locator = TermRecordLocator(
            row_kind=TermRowKind.V1,
            file_digest="a" * 64,
            row_ordinal=1,
            row_digest="b" * 64,
            record_id="term-1",
        )

        invalid_calls = (
            lambda: TermRecordLocator(
                row_kind=TermRowKind.LEGACY,
                file_digest="not-sha256",
                row_ordinal=0,
                row_digest="b" * 64,
                record_id=None,
            ),
            lambda: TermRecordLocator(
                row_kind=TermRowKind.LEGACY,
                file_digest="a" * 64,
                row_ordinal=-1,
                row_digest="b" * 64,
                record_id=None,
            ),
            lambda: TermRecordLocator(
                row_kind=TermRowKind.LEGACY,
                file_digest="a" * 64,
                row_ordinal=0,
                row_digest="b" * 64,
                record_id="legacy-id",
            ),
            lambda: TermRecordLocator(
                row_kind=TermRowKind.V1,
                file_digest="a" * 64,
                row_ordinal=0,
                row_digest="b" * 64,
                record_id=None,
            ),
            lambda: TermRecord(
                locator=valid_legacy_locator,
                record_id=None,
                source="legacy",
                target="target",
                policy=TermMatchPolicy.LEGACY,
                match_case=False,
                whole_word=None,
            ),
            lambda: TermRecord(
                locator=valid_v1_locator,
                record_id="other-id",
                source="configured",
                target="target",
                policy=TermMatchPolicy.CONFIGURED,
                match_case=False,
                whole_word=True,
            ),
            lambda: TermRecord(
                locator=valid_v1_locator,
                record_id="term-1",
                source="configured",
                target="target",
                policy=TermMatchPolicy.LEGACY,
                match_case=False,
                whole_word=True,
            ),
            lambda: TermRecord(
                locator=valid_v1_locator,
                record_id="term-1",
                source="",
                target="target",
                policy=TermMatchPolicy.CONFIGURED,
                match_case=False,
                whole_word=True,
            ),
            lambda: TermDraft(
                source="source",
                target="target",
                match_case=0,  # type: ignore[arg-type]
            ),
            lambda: LegacyTermRow(
                source="source",
                target="target",
                input_ordinal=-1,
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_call()

    def test_term_mutation_contracts_reject_ambiguous_commit_states(self) -> None:
        record = TermRecord(
            locator=TermRecordLocator(
                row_kind=TermRowKind.LEGACY,
                file_digest="a" * 64,
                row_ordinal=0,
                row_digest="b" * 64,
                record_id=None,
            ),
            record_id=None,
            source="source",
            target="target",
            policy=TermMatchPolicy.LEGACY,
            match_case=None,
            whole_word=None,
        )
        report = TermMutationReport(
            action="update",
            resource_path=Path("/tmp/terms.csv"),
            committed_digest="c" * 64,
            records=(record,),
            created=0,
            updated=1,
            deleted=0,
            imported=0,
            overwritten=0,
        )
        valid_outcome_fields = {
            "report": None,
            "error_code": "COMMIT_FAILED",
            "retryable": True,
            "recovery_path": Path("/tmp/.terms.csv.recovery"),
            "quarantined": False,
            "safe_detail": "Retry the operation.",
        }

        invalid_calls = (
            lambda: PreparedTermMutation(
                action="update",
                resource_path=Path("/tmp/terms.csv"),
                base_digest="a" * 64,
                staged_path=Path("/tmp/terms.csv"),
                recovery_path=None,
                candidate_records=(record,),
            ),
            lambda: PreparedTermMutation(
                action="update",
                resource_path=Path("/tmp/terms.csv"),
                base_digest="a" * 64,
                staged_path=Path("/var/tmp/.terms.csv.stage"),
                recovery_path=None,
                candidate_records=(record,),
            ),
            lambda: PreparedTermMutation(
                action="update",
                resource_path=Path("/tmp/terms.csv"),
                base_digest="a" * 64,
                staged_path=Path("/tmp/.terms.csv.stage"),
                recovery_path=Path("/tmp/.terms.csv.stage"),
                candidate_records=(record,),
            ),
            lambda: PreparedTermMutation(
                action="update",
                resource_path=Path("/tmp/terms.csv"),
                base_digest="a" * 64,
                staged_path=Path("/tmp/.terms.csv.stage"),
                recovery_path=None,
                candidate_records=[record],  # type: ignore[arg-type]
            ),
            lambda: TermMutationReport(
                action="update",
                resource_path=Path("/tmp/terms.csv"),
                committed_digest="c" * 64,
                records=(record,),
                created=0,
                updated=-1,
                deleted=0,
                imported=0,
                overwritten=0,
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.COMMITTED,
                **valid_outcome_fields,
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.NOT_COMMITTED,
                report=report,
                error_code="COMMIT_FAILED",
                retryable=True,
                recovery_path=None,
                quarantined=False,
                safe_detail="Retry the operation.",
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.ROLLED_BACK,
                report=None,
                error_code=None,
                retryable=True,
                recovery_path=None,
                quarantined=False,
                safe_detail="Retry the operation.",
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.ROLLED_BACK,
                report=None,
                error_code="ROLLBACK_COMPLETED",
                retryable=True,
                recovery_path=None,
                quarantined=True,
                safe_detail="Retry the operation.",
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.INDETERMINATE,
                report=None,
                error_code="ROLLBACK_FAILED",
                retryable=False,
                recovery_path=None,
                quarantined=True,
                safe_detail="Restore the recovery file.",
            ),
            lambda: TermCommitOutcome(
                state=TermCommitState.INDETERMINATE,
                report=None,
                error_code="ROLLBACK_FAILED",
                retryable=True,
                recovery_path=Path("/tmp/.terms.csv.recovery"),
                quarantined=True,
                safe_detail="Restore the recovery file.",
            ),
            lambda: TermCleanupReport(
                cleaned=True,
                recovery_path=Path("/tmp/.terms.csv.recovery"),
                warning_code=None,
            ),
            lambda: TermCleanupReport(
                cleaned=False,
                recovery_path=None,
                warning_code="RECOVERY_DELETE_FAILED",
            ),
        )

        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_call()


if __name__ == "__main__":
    unittest.main()
