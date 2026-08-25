"""Pure, deterministic inventory of normalized project raw speakers."""

from __future__ import annotations

from editor_contracts import (
    EditorProject,
    SpeakerInventory,
    SpeakerInventoryItem,
)


def build_speaker_inventory(project: EditorProject) -> SpeakerInventory:
    """Return first-occurrence speaker counts without mutating the project."""

    counts: dict[str, int] = {}
    first_occurrences: dict[str, tuple[str, int]] = {}
    empty_count = 0

    for index, segment in enumerate(project.segments):
        raw_speaker = segment.speaker
        if not raw_speaker:
            empty_count += 1
            continue
        if raw_speaker not in counts:
            counts[raw_speaker] = 0
            first_occurrences[raw_speaker] = (segment.id, index)
        counts[raw_speaker] += 1

    items = tuple(
        SpeakerInventoryItem(
            raw_speaker=raw_speaker,
            count=count,
            first_segment_id=first_occurrences[raw_speaker][0],
            first_index=first_occurrences[raw_speaker][1],
        )
        for raw_speaker, count in counts.items()
    )
    return SpeakerInventory(
        items=items,
        empty_count=empty_count,
        segment_count=len(project.segments),
    )
