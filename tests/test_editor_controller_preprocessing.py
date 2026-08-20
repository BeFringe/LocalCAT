"""Controller lifecycle and atomicity tests for target preprocessing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from editor_contracts import (
    EditorProject,
    EditorSegment,
    LiteralReplaceRule,
    PreprocessPreferences,
)
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository


class EditorControllerPreprocessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="localcat-preprocessing-controller-"
        )
        self.root = Path(self.temporary.name)
        self.controller = EditorController(
            ResourceRepository(self.root / "app-data")
        )
        self.project_path = self.root / "project.json"
        self.controller.set_project(
            EditorProject(
                name="Preprocessing",
                path=self.project_path,
                segments=(
                    EditorSegment(
                        id="seg-1",
                        source="Source one",
                        target="alpha foo",
                        speaker="Alice",
                        confirmed=True,
                    ),
                    EditorSegment(
                        id="seg-2",
                        source="Source two",
                        target="second foo",
                        speaker="Bob",
                        confirmed=False,
                    ),
                    EditorSegment(
                        id="seg-3",
                        source="Source three",
                        target="untouched",
                        speaker="",
                        confirmed=True,
                    ),
                ),
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _rules(
        find: str = "foo",
        replacement: str = "bar",
    ) -> tuple[LiteralReplaceRule, ...]:
        return (
            LiteralReplaceRule(
                find=find,
                replacement=replacement,
                enabled=True,
            ),
        )

    def test_preview_is_read_only_and_apply_is_one_revision(self) -> None:
        before = self.controller.project
        revision = self.controller.project_revision

        preview = self.controller.preview_preprocessing(self._rules())

        self.assertIs(self.controller.project, before)
        self.assertEqual(self.controller.project_revision, revision)
        self.assertFalse(self.controller.dirty)
        self.assertEqual(
            tuple(change.segment_id for change in preview.changes),
            ("seg-1", "seg-2"),
        )

        report = self.controller.apply_preprocessing(preview)

        self.assertEqual(report.operation, "apply")
        self.assertEqual(report.resulting_revision, revision + 1)
        self.assertEqual(self.controller.project_revision, revision + 1)
        self.assertTrue(report.dirty)
        self.assertTrue(self.controller.dirty)
        self.assertEqual(
            tuple(segment.target for segment in self.controller.project.segments),
            ("alpha bar", "second bar", "untouched"),
        )
        self.assertEqual(
            tuple(segment.confirmed for segment in self.controller.project.segments),
            (False, False, True),
        )
        self.assertEqual(
            tuple(segment.source for segment in self.controller.project.segments),
            ("Source one", "Source two", "Source three"),
        )
        self.assertEqual(
            tuple(segment.speaker for segment in self.controller.project.segments),
            ("Alice", "Bob", ""),
        )
        self.assertTrue(self.controller.has_preprocessing_undo)

    def test_navigation_save_and_confirmation_follow_revision_lifecycle(self) -> None:
        self.assertEqual(self.controller.project_revision, 0)
        self.controller.go_to(1)
        self.assertEqual(self.controller.project_revision, 0)

        result = self.controller.confirm_current()

        self.assertTrue(result.write_report.succeeded)
        self.assertEqual(self.controller.project_revision, 1)
        self.assertTrue(self.controller.dirty)
        _ = self.controller.save_project(self.project_path)
        self.assertEqual(self.controller.project_revision, 1)
        self.assertFalse(self.controller.dirty)

    def test_undo_restores_clean_project_and_consumes_the_point(self) -> None:
        preview = self.controller.preview_preprocessing(self._rules())
        _ = self.controller.apply_preprocessing(preview)

        report = self.controller.undo_latest_preprocessing()

        self.assertEqual(report.operation, "undo")
        self.assertEqual(report.resulting_revision, 2)
        self.assertFalse(report.dirty)
        self.assertFalse(self.controller.dirty)
        self.assertEqual(
            tuple(segment.target for segment in self.controller.project.segments),
            ("alpha foo", "second foo", "untouched"),
        )
        self.assertEqual(
            tuple(segment.confirmed for segment in self.controller.project.segments),
            (True, False, True),
        )
        self.assertFalse(self.controller.has_preprocessing_undo)
        with self.assertRaisesRegex(EditorControllerError, "PREPROCESS.NO_UNDO"):
            self.controller.undo_latest_preprocessing()

    def test_stale_revision_and_complete_before_state_are_rejected_atomically(self) -> None:
        preview = self.controller.preview_preprocessing(self._rules())
        self.controller.update_target("manually changed")
        changed_project = self.controller.project

        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.STALE_REVISION",
        ):
            self.controller.apply_preprocessing(preview)

        self.assertIs(self.controller.project, changed_project)
        current_preview = self.controller.preview_preprocessing(self._rules())
        forged = replace(
            current_preview,
            changes=(
                replace(
                    current_preview.changes[0],
                    before_confirmed=not current_preview.changes[0].before_confirmed,
                ),
            ) + current_preview.changes[1:],
        )
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.STALE_SEGMENT",
        ):
            self.controller.apply_preprocessing(forged)
        self.assertIs(self.controller.project, changed_project)

    def test_unrelated_edit_is_preserved_but_related_edit_rejects_whole_undo(self) -> None:
        preview = self.controller.preview_preprocessing(
            self._rules("alpha", "ALPHA")
        )
        _ = self.controller.apply_preprocessing(preview)
        self.controller.go_to(2)
        self.controller.update_target("unrelated edit")

        report = self.controller.undo_latest_preprocessing()

        self.assertTrue(report.dirty)
        self.assertEqual(self.controller.project.segments[0].target, "alpha foo")
        self.assertTrue(self.controller.project.segments[0].confirmed)
        self.assertEqual(
            self.controller.project.segments[2].target,
            "unrelated edit",
        )

        self.controller.go_to(0)
        next_preview = self.controller.preview_preprocessing(
            self._rules("alpha", "ALPHA")
        )
        _ = self.controller.apply_preprocessing(next_preview)
        self.controller.update_target("related edit")
        project_after_related_edit = self.controller.project
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.STALE_UNDO",
        ):
            self.controller.undo_latest_preprocessing()
        self.assertIs(self.controller.project, project_after_related_edit)
        self.assertEqual(
            self.controller.project.segments[0].target,
            "related edit",
        )

    def test_new_batch_replaces_old_undo_and_save_after_apply_changes_baseline(self) -> None:
        first = self.controller.preview_preprocessing(self._rules())
        _ = self.controller.apply_preprocessing(first)
        second = self.controller.preview_preprocessing(self._rules("bar", "baz"))
        _ = self.controller.apply_preprocessing(second)

        _ = self.controller.undo_latest_preprocessing()

        self.assertEqual(self.controller.project.segments[0].target, "alpha bar")
        with self.assertRaisesRegex(EditorControllerError, "PREPROCESS.NO_UNDO"):
            self.controller.undo_latest_preprocessing()

        third = self.controller.preview_preprocessing(self._rules("bar", "baz"))
        _ = self.controller.apply_preprocessing(third)
        _ = self.controller.save_project(self.project_path)
        self.assertFalse(self.controller.dirty)

        report = self.controller.undo_latest_preprocessing()

        self.assertTrue(report.dirty)
        self.assertTrue(self.controller.dirty)
        self.assertEqual(self.controller.project.segments[0].target, "alpha bar")

    def test_switch_close_and_non_json_gate_isolate_batch_state(self) -> None:
        preview = self.controller.preview_preprocessing(self._rules())
        _ = self.controller.apply_preprocessing(preview)
        first_session = self.controller.project_session_id

        self.controller.set_project(
            EditorProject(
                name="Other",
                path=self.root / "other.json",
                segments=(
                    EditorSegment(id="other", source="Other", target="foo"),
                ),
            )
        )

        self.assertNotEqual(self.controller.project_session_id, first_session)
        self.assertFalse(self.controller.has_preprocessing_undo)
        with self.assertRaisesRegex(EditorControllerError, "PREPROCESS.NO_UNDO"):
            self.controller.undo_latest_preprocessing()
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.STALE_PROJECT_SESSION",
        ):
            self.controller.apply_preprocessing(preview)

        self.controller.close_project()
        self.assertFalse(self.controller.has_preprocessing_undo)
        self.controller.set_project(
            EditorProject(
                name="Text",
                path=self.root / "project.txt",
                segments=(
                    EditorSegment(id="text", source="Text", target="foo"),
                ),
            )
        )
        with self.assertRaisesRegex(
            EditorControllerError,
            "PROJECT_TOOLS.JSON_REQUIRED",
        ):
            self.controller.preview_preprocessing(self._rules())

    def test_preview_status_filter_and_no_selection_use_stable_errors(self) -> None:
        draft_only = self.controller.preview_preprocessing(
            self._rules(),
            include_draft=True,
            include_confirmed=False,
        )
        confirmed_only = self.controller.preview_preprocessing(
            self._rules(),
            include_draft=False,
            include_confirmed=True,
        )

        self.assertEqual(
            tuple(change.segment_id for change in draft_only.changes),
            ("seg-2",),
        )
        self.assertEqual(
            tuple(change.segment_id for change in confirmed_only.changes),
            ("seg-1",),
        )
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.NO_SELECTED_STATUS",
        ):
            self.controller.preview_preprocessing(
                self._rules(),
                include_draft=False,
                include_confirmed=False,
            )
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.INVALID_STATUS_SELECTION",
        ):
            self.controller.preview_preprocessing(
                self._rules(),
                include_draft=1,  # type: ignore[arg-type]
                include_confirmed=True,
            )

    def test_preprocess_preferences_are_defensive_and_do_not_mutate_project_state(
        self,
    ) -> None:
        preview = self.controller.preview_preprocessing(
            self._rules("alpha", "ALPHA")
        )
        _ = self.controller.apply_preprocessing(preview)
        project_before = self.controller.project
        revision_before = self.controller.project_revision
        dirty_before = self.controller.dirty
        session_before = self.controller.project_session_id
        index_before = self.controller.current_index
        preferences = PreprocessPreferences(
            rules=(
                LiteralReplaceRule(
                    find="first",
                    replacement="one",
                    enabled=False,
                ),
                LiteralReplaceRule(
                    find="second",
                    replacement="two",
                    enabled=True,
                ),
            ),
            include_draft=False,
            include_confirmed=True,
        )

        saved = self.controller.update_preprocess_preferences(preferences)

        self.assertEqual(saved, preferences)
        self.assertIsNot(saved, preferences)
        self.assertIsNot(saved.rules[0], preferences.rules[0])
        self.assertIs(self.controller.project, project_before)
        self.assertEqual(self.controller.project_revision, revision_before)
        self.assertEqual(self.controller.dirty, dirty_before)
        self.assertEqual(self.controller.project_session_id, session_before)
        self.assertEqual(self.controller.current_index, index_before)
        self.assertTrue(self.controller.has_preprocessing_undo)

        object.__setattr__(saved.rules[0], "find", "tampered-return")
        object.__setattr__(preferences.rules[0], "find", "tampered-input")
        reread = self.controller.preprocess_preferences()
        self.assertEqual(reread.rules[0].find, "first")
        self.assertIsNot(
            reread.rules[0],
            self.controller.preprocess_preferences().rules[0],
        )

        restored = EditorController(
            ResourceRepository(self.root / "app-data")
        ).preprocess_preferences()
        self.assertEqual(restored.rules[0].find, "first")
        self.assertEqual(restored.rules[1].find, "second")
        self.assertFalse(restored.include_draft)
        self.assertTrue(restored.include_confirmed)

    def test_preprocess_preference_failure_preserves_last_known_good_and_project(
        self,
    ) -> None:
        previous = PreprocessPreferences(
            rules=(
                LiteralReplaceRule(
                    find="old",
                    replacement="kept",
                    enabled=True,
                ),
            ),
            include_draft=True,
            include_confirmed=False,
        )
        _ = self.controller.update_preprocess_preferences(previous)
        state_path = self.controller.workspace_state.state_path
        state_snapshot = state_path.read_bytes()
        project_before = self.controller.project
        revision_before = self.controller.project_revision
        dirty_before = self.controller.dirty
        candidate = PreprocessPreferences(
            rules=(
                LiteralReplaceRule(
                    find="new",
                    replacement="lost",
                    enabled=True,
                ),
            ),
            include_draft=False,
            include_confirmed=True,
        )

        with (
            mock.patch(
                "workspace_state.os.replace",
                side_effect=OSError("disk full / private path"),
            ),
            self.assertRaisesRegex(
                EditorControllerError,
                "PREPROCESS.PREFERENCES_SAVE_FAILED",
            ),
        ):
            self.controller.update_preprocess_preferences(candidate)

        self.assertEqual(self.controller.preprocess_preferences(), previous)
        self.assertEqual(state_path.read_bytes(), state_snapshot)
        self.assertIs(self.controller.project, project_before)
        self.assertEqual(self.controller.project_revision, revision_before)
        self.assertEqual(self.controller.dirty, dirty_before)

        malformed = object.__new__(PreprocessPreferences)
        object.__setattr__(malformed, "rules", ())
        object.__setattr__(malformed, "include_draft", False)
        object.__setattr__(malformed, "include_confirmed", False)
        with self.assertRaisesRegex(
            EditorControllerError,
            "PREPROCESS.PREFERENCES_INVALID",
        ):
            self.controller.update_preprocess_preferences(malformed)
        self.assertEqual(self.controller.preprocess_preferences(), previous)


if __name__ == "__main__":
    unittest.main()
