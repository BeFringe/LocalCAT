"""Qt-free persistence for recent projects and local display preferences."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import cast

from editor_contracts import (
    DEFAULT_EDITOR_FONT_SIZE,
    DisplayPreferences,
    RecentProject,
    SegmentDensity,
    TMPreferences,
    WorkspaceMode,
)


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
MAX_RECENT_PROJECTS = 10


class WorkspaceStateError(RuntimeError):
    """Raised when local workspace state cannot be written safely."""


class WorkspaceStateRepository:
    """Own recent-project positions and editor display preferences."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir.expanduser().resolve()
        self.state_path = self.config_dir / "workspace.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceStateError(f"unable to prepare workspace directory: {exc}") from exc
        self._recent: tuple[RecentProject, ...] = ()
        self._preferences = DisplayPreferences()
        self._tm_preferences = TMPreferences()
        if self.state_path.exists():
            try:
                (
                    self._recent,
                    self._preferences,
                    self._tm_preferences,
                ) = self._read_state()
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                LOGGER.warning("Ignoring invalid workspace state: %s", exc)

    def recent_projects(self) -> tuple[RecentProject, ...]:
        """Return most-recent-first project locations and positions."""

        return self._recent

    def find_project(self, path: Path) -> RecentProject | None:
        """Return one remembered project after normalizing its local path."""

        normalized = path.expanduser().resolve()
        return next((item for item in self._recent if item.path == normalized), None)

    def remember_project(self, path: Path, segment_id: str, index: int) -> RecentProject:
        """Move a project to the front and atomically persist its current position."""

        remembered = RecentProject(
            path=path.expanduser().resolve(),
            segment_id=segment_id,
            index=index,
        )
        updated = (
            remembered,
            *(item for item in self._recent if item.path != remembered.path),
        )[:MAX_RECENT_PROJECTS]
        self._write_state(updated, self._preferences, self._tm_preferences)
        self._recent = updated
        return remembered

    def remove_recent(self, path: Path) -> None:
        """Remove one stale recent-project entry without touching the project file."""

        normalized = path.expanduser().resolve()
        updated = tuple(item for item in self._recent if item.path != normalized)
        if updated == self._recent:
            return
        self._write_state(updated, self._preferences, self._tm_preferences)
        self._recent = updated

    def display_preferences(self) -> DisplayPreferences:
        """Return persisted editor-only display preferences."""

        return self._preferences

    def update_display_preferences(
        self,
        preferences: DisplayPreferences,
    ) -> DisplayPreferences:
        """Atomically persist validated display preferences."""

        if not isinstance(preferences, DisplayPreferences):
            raise WorkspaceStateError("display preferences contract is required")
        self._write_state(self._recent, preferences, self._tm_preferences)
        self._preferences = preferences
        return preferences

    def tm_preferences(self) -> TMPreferences:
        """Return persisted device-local TM query preferences."""

        return self._tm_preferences

    def update_tm_preferences(self, preferences: TMPreferences) -> TMPreferences:
        """Validate and atomically persist device-local TM query preferences."""

        if type(preferences) is not TMPreferences:
            raise WorkspaceStateError("TM preferences contract is required")
        try:
            validated = TMPreferences(
                minimum_similarity=preferences.minimum_similarity,
                result_limit=preferences.result_limit,
            )
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(f"invalid TM preferences: {exc}") from exc
        self._write_state(self._recent, self._preferences, validated)
        self._tm_preferences = validated
        return validated

    def _read_state(
        self,
    ) -> tuple[tuple[RecentProject, ...], DisplayPreferences, TMPreferences]:
        payload = cast(object, json.loads(self.state_path.read_text(encoding="utf-8")))
        if not isinstance(payload, dict):
            raise ValueError("workspace state root must be an object")
        mapping = cast(dict[str, object], payload)
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported workspace state schema")

        raw_recent = mapping.get("recent_projects", [])
        if not isinstance(raw_recent, list):
            raise ValueError("recent_projects must be an array")
        recent: list[RecentProject] = []
        for index, entry in enumerate(cast(list[object], raw_recent), start=1):
            if not isinstance(entry, dict):
                LOGGER.warning("Skipping invalid recent project entry %s", index)
                continue
            item = cast(dict[str, object], entry)
            try:
                path = item["path"]
                segment_id = item["segment_id"]
                position = item["index"]
                if not isinstance(path, str) or not isinstance(segment_id, str):
                    raise TypeError("recent project path and segment id must be strings")
                if not isinstance(position, int) or isinstance(position, bool):
                    raise TypeError("recent project index must be an integer")
                recent.append(
                    RecentProject(
                        path=Path(path),
                        segment_id=segment_id,
                        index=position,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("Skipping invalid recent project entry %s: %s", index, exc)

        raw_display = mapping.get("display", {})
        preferences = DisplayPreferences()
        if isinstance(raw_display, dict):
            display = cast(dict[str, object], raw_display)
            try:
                segment_density = SegmentDensity(
                    cast(
                        str,
                        display.get(
                            "segment_density",
                            SegmentDensity.COMPACT.value,
                        ),
                    )
                )
                workspace_mode = WorkspaceMode(
                    cast(
                        str,
                        display.get(
                            "workspace_mode",
                            WorkspaceMode.EDIT.value,
                        ),
                    )
                )
            except (TypeError, ValueError):
                LOGGER.warning("Using default display preferences from invalid workspace state")
            else:
                editor_font_size = DEFAULT_EDITOR_FONT_SIZE
                if "editor_font_size" in display:
                    try:
                        editor_font_size = DisplayPreferences(
                            editor_font_size=cast(int, display["editor_font_size"])
                        ).editor_font_size
                    except (TypeError, ValueError):
                        LOGGER.warning(
                            "Using default editor font size from invalid workspace state"
                        )
                preferences = DisplayPreferences(
                    segment_density=segment_density,
                    workspace_mode=workspace_mode,
                    editor_font_size=editor_font_size,
                )

        tm_preferences = TMPreferences()
        raw_tm_preferences = mapping.get("tm_preferences")
        if raw_tm_preferences is not None:
            if not isinstance(raw_tm_preferences, dict):
                LOGGER.warning("Using default TM preferences from invalid workspace state")
            else:
                tm_mapping = cast(dict[str, object], raw_tm_preferences)
                try:
                    tm_preferences = TMPreferences(
                        minimum_similarity=cast(
                            float,
                            tm_mapping.get("minimum_similarity", 0.60),
                        )
                    )
                except (TypeError, ValueError):
                    LOGGER.warning(
                        "Using default TM preferences from invalid workspace state"
                    )

        return (
            tuple(recent[:MAX_RECENT_PROJECTS]),
            preferences,
            tm_preferences,
        )

    def _write_state(
        self,
        recent: tuple[RecentProject, ...],
        preferences: DisplayPreferences,
        tm_preferences: TMPreferences,
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "recent_projects": [
                {
                    "path": str(item.path),
                    "segment_id": item.segment_id,
                    "index": item.index,
                }
                for item in recent
            ],
            "display": {
                "segment_density": preferences.segment_density.value,
                "workspace_mode": preferences.workspace_mode.value,
                "editor_font_size": preferences.editor_font_size,
            },
            "tm_preferences": {
                "minimum_similarity": tm_preferences.minimum_similarity,
            },
        }
        temp_path: Path | None = None
        try:
            rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.config_dir,
                prefix=".workspace.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                _ = handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise WorkspaceStateError(f"unable to write workspace state: {exc}") from exc
