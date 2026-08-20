from __future__ import annotations

import unittest

from editor_contracts import (
    EditorProject,
    EditorSegment,
    SpeakerInventory,
    SpeakerInventoryItem,
)
from speaker_inventory import build_speaker_inventory


class SpeakerInventoryTests(unittest.TestCase):
    def test_builds_first_occurrence_order_counts_and_empty_total(self) -> None:
        project = EditorProject(
            name="Speakers",
            segments=(
                EditorSegment(id="one", source="One", speaker="Alice"),
                EditorSegment(id="two", source="Two", speaker=""),
                EditorSegment(id="three", source="Three", speaker="Bob"),
                EditorSegment(id="four", source="Four", speaker="Alice"),
                EditorSegment(id="five", source="Five", speaker=""),
            ),
        )

        first = build_speaker_inventory(project)
        second = build_speaker_inventory(project)

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            SpeakerInventory(
                items=(
                    SpeakerInventoryItem("Alice", 2, "one", 0),
                    SpeakerInventoryItem("Bob", 1, "three", 2),
                ),
                empty_count=2,
                segment_count=5,
            ),
        )
        self.assertIs(project.segments[0].speaker, project.segments[0].speaker)
        self.assertEqual(tuple(segment.source for segment in project.segments), (
            "One", "Two", "Three", "Four", "Five"
        ))

    def test_does_not_infer_speaker_from_source(self) -> None:
        project = EditorProject(
            name="No inference",
            segments=(
                EditorSegment(id="one", source="Alice: Hello", speaker=""),
            ),
        )

        inventory = build_speaker_inventory(project)

        self.assertEqual(inventory.items, ())
        self.assertEqual(inventory.empty_count, 1)

    def test_contract_rejects_inconsistent_or_non_deterministic_shapes(self) -> None:
        first = SpeakerInventoryItem("Alice", 1, "one", 0)
        later = SpeakerInventoryItem("Bob", 1, "two", 1)
        invalid_builders = (
            lambda: SpeakerInventory((first, first), 0, 2),
            lambda: SpeakerInventory((later, first), 0, 2),
            lambda: SpeakerInventory((first,), 2, 2),
            lambda: SpeakerInventory((first,), 0, 0),
        )

        for builder in invalid_builders:
            with self.subTest(builder=builder):
                with self.assertRaises(ValueError):
                    builder()


if __name__ == "__main__":
    unittest.main()
