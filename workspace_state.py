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
    LiteralReplaceRule,
    PreprocessPreferences,
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


def _clone_preprocess_preferences(
    preferences: PreprocessPreferences,
) -> PreprocessPreferences:
    """Validate and detach the complete device-local preprocessing graph."""

    if type(preferences) is not PreprocessPreferences:
        raise TypeError("preprocess preferences contract is required")
    preferences.__post_init__()
    cloned = PreprocessPreferences(
        rules=tuple(
            LiteralReplaceRule(
                find=rule.find,
                replacement=rule.replacement,
                enabled=rule.enabled,
            )
            for rule in preferences.rules
        ),
        include_draft=preferences.include_draft,
        include_confirmed=preferences.include_confirmed,
    )
    cloned.__post_init__()
    return cloned


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
        self._preprocess_preferences = PreprocessPreferences()
        if self.state_path.exists():
            try:
                (
                    self._recent,
                    self._preferences,
                    self._tm_preferences,
                    self._preprocess_preferences,
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
        self._write_state(
            updated,
            self._preferences,
            self._tm_preferences,
            self._preprocess_preferences,
        )
        self._recent = updated
        return remembered

    def remove_recent(self, path: Path) -> None:
        """Remove one stale recent-project entry without touching the project file."""

        normalized = path.expanduser().resolve()
        updated = tuple(item for item in self._recent if item.path != normalized)
        if updated == self._recent:
            return
        self._write_state(
            updated,
            self._preferences,
            self._tm_preferences,
            self._preprocess_preferences,
        )
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
        self._write_state(
            self._recent,
            preferences,
            self._tm_preferences,
            self._preprocess_preferences,
        )
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
        self._write_state(
            self._recent,
            self._preferences,
            validated,
            self._preprocess_preferences,
        )
        self._tm_preferences = validated
        return validated

    def preprocess_preferences(self) -> PreprocessPreferences:
        """Return a detached device-local preprocessing preference graph."""

        return _clone_preprocess_preferences(self._preprocess_preferences)

    def update_preprocess_preferences(
        self,
        preferences: PreprocessPreferences,
    ) -> PreprocessPreferences:
        """Atomically replace preprocessing preferences after full validation."""

        try:
            validated = _clone_preprocess_preferences(preferences)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(
                f"invalid preprocess preferences: {exc}"
            ) from exc
        self._write_state(
            self._recent,
            self._preferences,
            self._tm_preferences,
            validated,
        )
        self._preprocess_preferences = validated
        return _clone_preprocess_preferences(validated)

    def _read_state(
        self,
    ) -> tuple[
        tuple[RecentProject, ...],
        DisplayPreferences,
        TMPreferences,
        PreprocessPreferences,
    ]:
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

        preprocess_preferences = PreprocessPreferences()
        raw_preprocessing = mapping.get("preprocessing")
        if raw_preprocessing is not None:
            try:
                if not isinstance(raw_preprocessing, dict):
                    raise TypeError("preprocessing must be an object")
                preprocessing = cast(dict[str, object], raw_preprocessing)
                if set(preprocessing) != {
                    "rules",
                    "include_draft",
                    "include_confirmed",
                }:
                    raise ValueError(
                        "preprocessing must contain the complete preference graph"
                    )
                raw_rules = preprocessing["rules"]
                include_draft = preprocessing["include_draft"]
                include_confirmed = preprocessing["include_confirmed"]
                if not isinstance(raw_rules, list):
                    raise TypeError("preprocessing rules must be an array")
                if type(include_draft) is not bool:
                    raise TypeError("preprocessing include_draft must be a boolean")
                if type(include_confirmed) is not bool:
                    raise TypeError(
                        "preprocessing include_confirmed must be a boolean"
                    )
                rules: list[LiteralReplaceRule] = []
                for raw_rule in cast(list[object], raw_rules):
                    if not isinstance(raw_rule, dict):
                        raise TypeError("preprocessing rule must be an object")
                    rule = cast(dict[str, object], raw_rule)
                    if set(rule) != {"find", "replacement", "enabled"}:
                        raise ValueError(
                            "preprocessing rule must contain find, replacement and enabled"
                        )
                    find = rule["find"]
                    replacement = rule["replacement"]
                    enabled = rule["enabled"]
                    if type(find) is not str or type(replacement) is not str:
                        raise TypeError(
                            "preprocessing rule text values must be strings"
                        )
                    if type(enabled) is not bool:
                        raise TypeError(
                            "preprocessing rule enabled must be a boolean"
                        )
                    rules.append(
                        LiteralReplaceRule(
                            find=find,
                            replacement=replacement,
                            enabled=enabled,
                        )
                    )
                preprocess_preferences = PreprocessPreferences(
                    rules=tuple(rules),
                    include_draft=include_draft,
                    include_confirmed=include_confirmed,
                )
            except (KeyError, TypeError, ValueError):
                LOGGER.warning(
                    "Using default preprocess preferences from invalid workspace state"
                )

        return (
            tuple(recent[:MAX_RECENT_PROJECTS]),
            preferences,
            tm_preferences,
            preprocess_preferences,
        )

    def _write_state(
        self,
        recent: tuple[RecentProject, ...],
        preferences: DisplayPreferences,
        tm_preferences: TMPreferences,
        preprocess_preferences: PreprocessPreferences,
    ) -> None:
        preprocess_preferences = _clone_preprocess_preferences(
            preprocess_preferences
        )
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
            "preprocessing": {
                "rules": [
                    {
                        "find": rule.find,
                        "replacement": rule.replacement,
                        "enabled": rule.enabled,
                    }
                    for rule in preprocess_preferences.rules
                ],
                "include_draft": preprocess_preferences.include_draft,
                "include_confirmed": preprocess_preferences.include_confirmed,
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
