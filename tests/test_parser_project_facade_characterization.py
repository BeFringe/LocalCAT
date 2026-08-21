from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editor_contracts import EditorProject, EditorSegment
from editor_controller import EditorController, EditorControllerError
from editor_project import ProjectError, load_project, save_project
from resource_repository import ResourceRepository


class ProjectFacadeCharacterizationTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8-sig",
        )

    def test_array_root_uses_compatibility_defaults_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "array-project.json"
            self._write_json(
                path,
                [
                    {
                        "id": "  explicit-id  ",
                        "source": "  First  source  ",
                        "target": "  First  target  ",
                        "speaker": "  Speaker  One  ",
                        "confirmed": True,
                    },
                    {
                        "id": "   ",
                        "source": "Second  source",
                        "target": None,
                        "speaker": None,
                    },
                    {"source": "Third source"},
                ],
            )

            project = load_project(path)

        self.assertEqual(project.name, "array-project")
        self.assertEqual(project.source_locale, "en-US")
        self.assertEqual(project.target_locale, "zh-CN")
        self.assertEqual(project.path, path.resolve())
        self.assertEqual(
            tuple(segment.id for segment in project.segments),
            ("explicit-id", "segment-2", "segment-3"),
        )
        self.assertEqual(
            tuple(segment.source for segment in project.segments),
            ("First  source", "Second  source", "Third source"),
        )
        self.assertEqual(project.segments[0].target, "First  target")
        self.assertEqual(project.segments[0].speaker, "Speaker  One")
        self.assertTrue(project.segments[0].confirmed)
        self.assertEqual(project.segments[1].target, "")
        self.assertEqual(project.segments[1].speaker, "")
        self.assertFalse(project.segments[1].confirmed)

    def test_object_root_defaults_blank_metadata_and_keeps_ids_document_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.json"
            second_path = root / "second.json"
            self._write_json(
                first_path,
                {
                    "schema_version": 1,
                    "name": "   ",
                    "source_locale": "   ",
                    "segments": [{"id": "shared", "source": " First "}],
                },
            )
            self._write_json(
                second_path,
                {
                    "name": " Second  chapter ",
                    "source_locale": " ja-JP ",
                    "target_locale": " zh-Hans ",
                    "segments": [{"id": "shared", "source": " Second "}],
                },
            )

            first = load_project(first_path)
            second = load_project(second_path)

        self.assertEqual(first.name, "first")
        self.assertEqual(first.source_locale, "en-US")
        self.assertEqual(first.target_locale, "zh-CN")
        self.assertEqual(first.segments[0].id, "shared")
        self.assertEqual(first.segments[0].source, "First")
        self.assertEqual(second.name, "Second  chapter")
        self.assertEqual(second.source_locale, "ja-JP")
        self.assertEqual(second.target_locale, "zh-Hans")
        self.assertEqual(second.segments[0].id, "shared")
        self.assertEqual(second.segments[0].source, "Second")

    def test_invalid_json_inputs_fail_as_whole_projects(self) -> None:
        invalid_payloads = {
            "scalar-root": "not-a-project",
            "missing-segments": {"name": "Missing"},
            "empty-project": {"segments": []},
            "non-object-segment": ["not-an-object"],
            "missing-source": [{"id": "one"}],
            "empty-source": [{"source": "   "}],
            "numeric-source": [{"source": 1}],
            "numeric-target": [{"source": "One", "target": 1}],
            "numeric-speaker": [{"source": "One", "speaker": 1}],
            "non-boolean-confirmed": [{"source": "One", "confirmed": 1}],
            "duplicate-id": [
                {"id": "same", "source": "One"},
                {"id": " same ", "source": "Two"},
            ],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case, payload in invalid_payloads.items():
                with self.subTest(case=case):
                    path = root / f"{case}.json"
                    self._write_json(path, payload)
                    with self.assertRaises(ProjectError):
                        load_project(path)

    def test_txt_is_source_only_with_dense_ids_and_empty_project_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.TXT"
            path.write_text(
                "\ufeff First line \n\n   \nSecond  line\r\n Third line ",
                encoding="utf-8",
            )
            empty_path = root / "empty.txt"
            empty_path.write_text("\ufeff\n \n\t\n", encoding="utf-8")

            project = load_project(path)
            with self.assertRaises(ProjectError):
                load_project(empty_path)

        self.assertEqual(project.name, "chapter")
        self.assertEqual(
            tuple(segment.id for segment in project.segments),
            ("segment-1", "segment-2", "segment-3"),
        )
        self.assertEqual(
            tuple(segment.source for segment in project.segments),
            ("First line", "Second  line", "Third line"),
        )
        self.assertTrue(all(segment.target == "" for segment in project.segments))
        self.assertTrue(all(segment.speaker == "" for segment in project.segments))
        self.assertTrue(all(not segment.confirmed for segment in project.segments))

    def test_save_emits_complete_v1_schema_in_segment_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "saved.json"
            project = EditorProject(
                name="Saved project",
                source_locale="en-GB",
                target_locale="zh-CN",
                segments=(
                    EditorSegment(
                        id="two",
                        source="Second",
                        target="第二",
                        speaker="B",
                        confirmed=False,
                    ),
                    EditorSegment(
                        id="one",
                        source="First",
                        target="第一",
                        speaker="A",
                        confirmed=True,
                    ),
                ),
            )

            result = save_project(project, path)
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))

        self.assertEqual(result, path.resolve())
        self.assertTrue(raw_bytes.endswith(b"\n"))
        self.assertEqual(
            list(payload),
            ["schema_version", "name", "source_locale", "target_locale", "segments"],
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            [segment["id"] for segment in payload["segments"]],
            ["two", "one"],
        )
        self.assertEqual(
            list(payload["segments"][0]),
            ["id", "source", "target", "speaker", "confirmed"],
        )
        self.assertEqual(payload["segments"][0]["target"], "第二")
        self.assertFalse(payload["segments"][0]["confirmed"])
        self.assertTrue(payload["segments"][1]["confirmed"])

    def test_failed_atomic_replace_preserves_target_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "protected.json"
            original_bytes = b'{"sentinel":"keep"}\n'
            path.write_bytes(original_bytes)
            project = EditorProject(
                name="Replacement",
                segments=(EditorSegment(id="one", source="Source"),),
            )

            with patch("editor_project.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(ProjectError):
                    save_project(project, path)

            preserved_bytes = path.read_bytes()
            remaining_names = tuple(candidate.name for candidate in root.iterdir())

        self.assertEqual(preserved_bytes, original_bytes)
        self.assertEqual(remaining_names, ("protected.json",))

    def test_controller_installs_only_success_and_clears_dirty_only_after_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = EditorController(ResourceRepository(root / "app-data"))
            valid_path = root / "valid.json"
            invalid_path = root / "invalid.json"
            output_path = root / "output.json"
            self._write_json(
                valid_path,
                {
                    "segments": [
                        {"id": "one", "source": "One"},
                        {"id": "two", "source": "Two"},
                    ]
                },
            )
            invalid_path.write_text('{"segments":[{"source":', encoding="utf-8")

            controller.open_project(valid_path)
            controller.go_to(1)
            controller.update_target("Draft")
            project_before_failure = controller.project
            index_before_failure = controller.current_index
            session_before_failure = controller.project_session_id
            epoch_before_failure = controller.query_epoch

            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT\.LOAD_FAILED$",
            ):
                controller.open_project(invalid_path)

            self.assertIs(controller.project, project_before_failure)
            self.assertEqual(controller.current_index, index_before_failure)
            self.assertEqual(controller.project_session_id, session_before_failure)
            self.assertEqual(controller.query_epoch, epoch_before_failure)
            self.assertTrue(controller.dirty)

            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT\.SAVE_FAILED$",
            ):
                controller.save_project(root / "unsupported.txt")

            self.assertIs(controller.project, project_before_failure)
            self.assertTrue(controller.dirty)

            saved = controller.save_project(output_path)

        self.assertEqual(saved.path, output_path.resolve())
        self.assertEqual(saved.segments[1].target, "Draft")
        self.assertFalse(controller.dirty)


if __name__ == "__main__":
    unittest.main()
