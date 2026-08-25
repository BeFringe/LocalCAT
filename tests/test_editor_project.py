from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import editor_project

from editor_contracts import EditorProject, EditorSegment
from editor_project import ProjectError, load_project, sample_project, save_project


class EditorProjectCodecTest(unittest.TestCase):
    def test_loads_json_array_and_preserves_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "chapter.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "line-a",
                            "source": "Hello",
                            "target": "你好",
                            "speaker": "Narrator",
                            "confirmed": True,
                        },
                        {"source": "World"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            project = load_project(path)

        self.assertEqual(project.name, "chapter")
        self.assertEqual(project.path, path.absolute())
        self.assertEqual(project.segments[0].id, "line-a")
        self.assertTrue(project.segments[0].confirmed)
        self.assertEqual(project.segments[1].id, "segment-2")

    def test_loads_versioned_project_and_txt_non_empty_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            json_path = root / "saved.json"
            json_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "Saved",
                        "source_locale": "en-GB",
                        "target_locale": "zh-CN",
                        "segments": [{"id": "1", "source": "One", "target": "一"}],
                    }
                ),
                encoding="utf-8",
            )
            txt_path = root / "plain.txt"
            txt_path.write_text("First\n\n  Second  \n", encoding="utf-8")

            saved = load_project(json_path)
            plain = load_project(txt_path)

        self.assertEqual(saved.source_locale, "en-GB")
        self.assertEqual(plain.segments[1].source, "Second")
        self.assertEqual(len(plain.segments), 2)

    def test_save_round_trip_uses_versioned_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.json"
            project = EditorProject(
                name="Demo",
                source_locale="en-US",
                target_locale="zh-CN",
                segments=(
                    EditorSegment(
                        id="1",
                        source="Hello",
                        target="你好",
                        speaker="N",
                        confirmed=True,
                    ),
                ),
            )

            result = save_project(project, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_project(path)

        self.assertEqual(result, path.absolute())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["segments"][0]["target"], "你好")
        self.assertEqual(loaded.segments, project.segments)

    def test_invalid_input_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid = root / "invalid.json"
            invalid.write_text('[{"source": ""}]', encoding="utf-8")
            output = root / "protected.txt"
            output.write_text("keep-me", encoding="utf-8")

            with self.assertRaises(ProjectError):
                load_project(invalid)
            with self.assertRaises(ProjectError):
                load_project(root / "missing.json")
            with self.assertRaises(ProjectError):
                load_project(root / "unsupported.po")
            with self.assertRaises(ProjectError):
                save_project(sample_project(), output)

            self.assertEqual(output.read_text(encoding="utf-8"), "keep-me")

    def test_sample_project_is_immediately_usable(self) -> None:
        project = sample_project()

        self.assertGreaterEqual(len(project.segments), 3)
        self.assertTrue(all(segment.source for segment in project.segments))
        self.assertIsNone(project.path)

    def test_facade_no_longer_exposes_private_localcat_grammar_or_writer_helpers(self) -> None:
        for name in (
            "_clean_string",
            "_segment_from_mapping",
            "_load_json_project",
            "_load_text_project",
            "_project_payload",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(editor_project, name))


if __name__ == "__main__":
    unittest.main()
