"""Pure-function tests for ordered target-only preprocessing previews."""

from __future__ import annotations

import unittest

from editor_contracts import EditorProject, EditorSegment, LiteralReplaceRule
from target_preprocessor import PreprocessValidationError, preview_preprocessing


class TargetPreprocessorTests(unittest.TestCase):
    def test_preview_applies_enabled_rules_in_order_and_preserves_project(self) -> None:
        project = EditorProject(
            name="Ordered",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="Keep source one",
                    target="a FOO aaaa",
                    speaker="Alice",
                    confirmed=True,
                ),
                EditorSegment(
                    id="seg-2",
                    source="Keep source two",
                    target="untouched",
                    speaker="Bob",
                    confirmed=True,
                ),
                EditorSegment(
                    id="seg-3",
                    source="Keep source three",
                    target="",
                    speaker="",
                    confirmed=True,
                ),
            ),
        )
        original_project = project
        rules = (
            LiteralReplaceRule(find="a", replacement="ab", enabled=True),
            LiteralReplaceRule(find="b", replacement="c", enabled=True),
            LiteralReplaceRule(find="FOO", replacement="ignored", enabled=False),
        )

        preview = preview_preprocessing(project, "session-1", 7, rules)

        self.assertEqual(preview.project_session_id, "session-1")
        self.assertEqual(preview.base_revision, 7)
        self.assertEqual(len(preview.changes), 1)
        change = preview.changes[0]
        self.assertEqual(change.segment_id, "seg-1")
        self.assertEqual(change.segment_index, 0)
        self.assertEqual(change.before_target, "a FOO aaaa")
        self.assertEqual(change.after_target, "ac FOO acacacac")
        self.assertTrue(change.before_confirmed)
        self.assertFalse(change.after_confirmed)
        self.assertIs(project, original_project)
        self.assertEqual(project.segments[0].target, "a FOO aaaa")
        self.assertTrue(project.segments[0].confirmed)
        self.assertEqual(project.segments[1].target, "untouched")
        self.assertTrue(project.segments[1].confirmed)
        self.assertEqual(project.segments[2].target, "")
        self.assertTrue(project.segments[2].confirmed)
        self.assertEqual(
            tuple(segment.id for segment in project.segments),
            ("seg-1", "seg-2", "seg-3"),
        )
        self.assertEqual(
            tuple(segment.source for segment in project.segments),
            ("Keep source one", "Keep source two", "Keep source three"),
        )
        self.assertEqual(
            tuple(segment.speaker for segment in project.segments),
            ("Alice", "Bob", ""),
        )

    def test_each_rule_is_non_overlapping_and_the_rule_group_is_not_rerun(self) -> None:
        project = EditorProject(
            name="One pass",
            segments=(
                EditorSegment(id="seg-1", source="Source", target="aaaa"),
                EditorSegment(id="seg-2", source="Source", target="a"),
            ),
        )
        rules = (
            LiteralReplaceRule(find="aa", replacement="b", enabled=True),
            LiteralReplaceRule(find="a", replacement="ab", enabled=True),
        )

        preview = preview_preprocessing(project, "session-2", 0, rules)

        self.assertEqual(
            tuple(change.after_target for change in preview.changes),
            ("bb", "ab"),
        )

    def test_literal_matching_is_case_sensitive_without_unicode_normalization(self) -> None:
        project = EditorProject(
            name="Literal",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="Source",
                    target="A.b e\u0301 É",
                    confirmed=False,
                ),
            ),
        )
        rules = (
            LiteralReplaceRule(find=".", replacement="!", enabled=True),
            LiteralReplaceRule(find="é", replacement="e", enabled=True),
            LiteralReplaceRule(find="A.*b", replacement="regex", enabled=True),
        )

        preview = preview_preprocessing(project, "session-3", 4, rules)

        self.assertEqual(preview.changes[0].after_target, "A!b e\u0301 É")
        self.assertFalse(preview.changes[0].before_confirmed)
        self.assertFalse(preview.changes[0].after_confirmed)

    def test_empty_find_is_rejected_by_contract_and_preprocessor_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "find value must not be empty"):
            LiteralReplaceRule(find="", replacement="x", enabled=True)

        malformed_rule = object.__new__(LiteralReplaceRule)
        object.__setattr__(malformed_rule, "find", "")
        object.__setattr__(malformed_rule, "replacement", "x")
        object.__setattr__(malformed_rule, "enabled", True)
        project = EditorProject(
            name="Malformed",
            segments=(EditorSegment(id="seg-1", source="Source", target="text"),),
        )

        with self.assertRaises(PreprocessValidationError) as raised:
            preview_preprocessing(project, "session-4", 1, (malformed_rule,))

        self.assertEqual(raised.exception.code, "EMPTY_FIND")

    def test_no_enabled_rules_are_rejected_without_project_changes(self) -> None:
        project = EditorProject(
            name="Disabled",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="Source",
                    target="find me",
                    confirmed=True,
                ),
            ),
        )
        rules = (
            LiteralReplaceRule(find="find", replacement="replace", enabled=False),
        )

        with self.assertRaises(PreprocessValidationError) as raised:
            preview_preprocessing(project, "session-5", 2, rules)

        self.assertEqual(raised.exception.code, "NO_ENABLED_RULES")
        self.assertEqual(project.segments[0].target, "find me")
        self.assertTrue(project.segments[0].confirmed)

    def test_no_actual_change_including_empty_targets_is_rejected(self) -> None:
        project = EditorProject(
            name="No changes",
            segments=(
                EditorSegment(
                    id="seg-1",
                    source="Source",
                    target="",
                    confirmed=True,
                ),
                EditorSegment(
                    id="seg-2",
                    source="Source",
                    target="same",
                    confirmed=False,
                ),
            ),
        )
        rules = (
            LiteralReplaceRule(find="missing", replacement="value", enabled=True),
            LiteralReplaceRule(find="same", replacement="same", enabled=True),
        )

        with self.assertRaises(PreprocessValidationError) as raised:
            preview_preprocessing(project, "session-6", 3, rules)

        self.assertEqual(raised.exception.code, "NO_CHANGES")
        self.assertEqual(tuple(segment.target for segment in project.segments), ("", "same"))
        self.assertEqual(
            tuple(segment.confirmed for segment in project.segments),
            (True, False),
        )

    def test_status_selection_filters_before_literal_replacement(self) -> None:
        project = EditorProject(
            name="Status filter",
            segments=(
                EditorSegment(
                    id="draft",
                    source="Draft source",
                    target="foo draft",
                    confirmed=False,
                ),
                EditorSegment(
                    id="confirmed",
                    source="Confirmed source",
                    target="foo confirmed",
                    confirmed=True,
                ),
            ),
        )
        rules = (
            LiteralReplaceRule(find="foo", replacement="bar", enabled=True),
        )

        draft_only = preview_preprocessing(
            project,
            "session-draft",
            2,
            rules,
            include_draft=True,
            include_confirmed=False,
        )
        confirmed_only = preview_preprocessing(
            project,
            "session-confirmed",
            2,
            rules,
            include_draft=False,
            include_confirmed=True,
        )

        self.assertEqual(
            tuple(change.segment_id for change in draft_only.changes),
            ("draft",),
        )
        self.assertFalse(draft_only.changes[0].before_confirmed)
        self.assertEqual(
            tuple(change.segment_id for change in confirmed_only.changes),
            ("confirmed",),
        )
        self.assertTrue(confirmed_only.changes[0].before_confirmed)
        self.assertIs(project.segments[0].confirmed, False)
        self.assertIs(project.segments[1].confirmed, True)

    def test_no_selected_status_and_foreign_boolean_are_structured_errors(self) -> None:
        project = EditorProject(
            name="Invalid status",
            segments=(EditorSegment(id="seg", source="Source", target="foo"),),
        )
        rules = (
            LiteralReplaceRule(find="foo", replacement="bar", enabled=True),
        )

        with self.assertRaises(PreprocessValidationError) as no_status:
            preview_preprocessing(
                project,
                "session-none",
                0,
                rules,
                include_draft=False,
                include_confirmed=False,
            )
        self.assertEqual(no_status.exception.code, "NO_SELECTED_STATUS")

        with self.assertRaises(PreprocessValidationError) as invalid_status:
            preview_preprocessing(
                project,
                "session-invalid",
                0,
                rules,
                include_draft=1,  # type: ignore[arg-type]
                include_confirmed=True,
            )
        self.assertEqual(
            invalid_status.exception.code,
            "INVALID_STATUS_SELECTION",
        )
        self.assertEqual(project.segments[0].target, "foo")


if __name__ == "__main__":
    unittest.main()
