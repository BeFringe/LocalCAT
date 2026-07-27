from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from editor_contracts import (
    ConfirmResult,
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


if __name__ == "__main__":
    unittest.main()
