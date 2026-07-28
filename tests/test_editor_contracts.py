from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from editor_contracts import (
    DEFAULT_EDITOR_FONT_SIZE,
    MAX_EDITOR_FONT_SIZE,
    MIN_EDITOR_FONT_SIZE,
    ConfirmResult,
    DisplayPreferences,
    EditorProject,
    EditorSegment,
    ImportReport,
    ImportRequest,
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


if __name__ == "__main__":
    unittest.main()
