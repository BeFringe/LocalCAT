from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository


class EditorControllerSpeakerInventoryTests(unittest.TestCase):
    def test_json_inventory_is_repeatable_read_only_and_keeps_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "speakers.json"
            path.write_text(
                json.dumps(
                    {
                        "name": "Speakers",
                        "segments": [
                            {"id": "one", "source": "One", "speaker": "Alice"},
                            {"id": "two", "source": "Two", "speaker": ""},
                            {"id": "three", "source": "Three", "speaker": "Alice"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller = EditorController(ResourceRepository(root / "data"))
            controller.open_project(path)
            controller.go_to(2)
            controller.update_target("Unsaved")
            before = controller.project
            before_index = controller.current_index
            before_dirty = controller.dirty

            first = controller.speaker_inventory()
            second = controller.speaker_inventory()

        self.assertEqual(first, second)
        self.assertEqual(tuple(item.raw_speaker for item in first.items), ("Alice",))
        self.assertEqual(first.items[0].count, 2)
        self.assertEqual(first.empty_count, 1)
        self.assertIs(controller.project, before)
        self.assertEqual(controller.current_index, before_index)
        self.assertEqual(controller.dirty, before_dirty)

    def test_non_json_and_no_project_fail_closed_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = EditorController(ResourceRepository(root / "data"))
            with self.assertRaisesRegex(
                EditorControllerError,
                "PROJECT_TOOLS.NO_PROJECT",
            ):
                controller.speaker_inventory()

            path = root / "notes.txt"
            path.write_text("Alice: hello\n", encoding="utf-8")
            controller.open_project(path)
            before = controller.project
            with self.assertRaisesRegex(
                EditorControllerError,
                "PROJECT_TOOLS.JSON_REQUIRED",
            ):
                controller.speaker_inventory()

        self.assertIs(controller.project, before)
        self.assertFalse(controller.dirty)


if __name__ == "__main__":
    unittest.main()
