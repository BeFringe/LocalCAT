from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from editor_contracts import (
    BrowseGroupDisplayMode,
    BrowseGroupPreferences,
    DEFAULT_EDITOR_FONT_SIZE,
    DisplayPreferences,
    LiteralReplaceRule,
    PreprocessPreferences,
    SegmentDensity,
    WorkspaceMode,
)
from workspace_state import WorkspaceStateError, WorkspaceStateRepository


class WorkspaceStateRepositoryTest(unittest.TestCase):
    def test_recent_projects_are_bounded_ordered_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = WorkspaceStateRepository(root / "app-data")
            paths: list[Path] = []
            for index in range(12):
                path = (root / f"project-{index}.json").resolve()
                path.write_text("{}", encoding="utf-8")
                paths.append(path)
                repository.remember_project(path, f"segment-{index}", index)

            recent = repository.recent_projects()
            restored = WorkspaceStateRepository(root / "app-data")

            self.assertEqual(len(recent), 10)
            self.assertEqual(recent[0].path, paths[-1])
            self.assertEqual(recent[-1].path, paths[2])
            self.assertEqual(restored.recent_projects(), recent)

    def test_display_preferences_round_trip_and_invalid_state_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            state_path.write_text("{not-json", encoding="utf-8")

            repository = WorkspaceStateRepository(config_dir)
            self.assertEqual(repository.display_preferences(), DisplayPreferences())
            self.assertEqual(state_path.read_text(encoding="utf-8"), "{not-json")

            preferences = DisplayPreferences(
                segment_density=SegmentDensity.WRAPPED,
                workspace_mode=WorkspaceMode.BROWSE,
                editor_font_size=22,
                browse_grouping=BrowseGroupPreferences(
                    enabled=False,
                    segments_per_group=40,
                    activation_group_threshold=8,
                    activation_segment_threshold=240,
                    display_mode=BrowseGroupDisplayMode.FIXED,
                ),
            )
            repository.update_display_preferences(preferences)
            restored = WorkspaceStateRepository(config_dir)
            payload = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(restored.display_preferences(), preferences)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["display"]["segment_density"], "wrapped")
            self.assertEqual(payload["display"]["workspace_mode"], "browse")
            self.assertEqual(payload["display"]["editor_font_size"], 22)
            self.assertEqual(
                payload["display"]["browse_grouping"],
                {
                    "enabled": False,
                    "segments_per_group": 40,
                    "activation_group_threshold": 8,
                    "activation_segment_threshold": 240,
                    "display_mode": "fixed",
                },
            )

    def test_invalid_browse_grouping_falls_back_without_losing_display(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            payload = {
                "schema_version": 1,
                "recent_projects": [],
                "display": {
                    "segment_density": "wrapped",
                    "workspace_mode": "browse",
                    "editor_font_size": 20,
                    "browse_grouping": {
                        "enabled": True,
                        "segments_per_group": 25,
                        "activation_group_threshold": 5,
                        "activation_segment_threshold": 100,
                    },
                },
            }
            rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            state_path.write_text(rendered, encoding="utf-8")

            with self.assertLogs("workspace_state", level="WARNING"):
                preferences = WorkspaceStateRepository(
                    config_dir
                ).display_preferences()

            self.assertIs(preferences.segment_density, SegmentDensity.WRAPPED)
            self.assertIs(preferences.workspace_mode, WorkspaceMode.BROWSE)
            self.assertEqual(preferences.editor_font_size, 20)
            self.assertEqual(
                preferences.browse_grouping,
                BrowseGroupPreferences(),
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), rendered)

    def test_valid_legacy_browse_grouping_without_mode_preserves_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "recent_projects": [],
                        "display": {
                            "browse_grouping": {
                                "enabled": False,
                                "segments_per_group": 60,
                                "activation_group_threshold": 9,
                                "activation_segment_threshold": 300,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            preferences = WorkspaceStateRepository(
                config_dir
            ).display_preferences().browse_grouping

            self.assertEqual(
                preferences,
                BrowseGroupPreferences(
                    enabled=False,
                    segments_per_group=60,
                    activation_group_threshold=9,
                    activation_segment_threshold=300,
                    display_mode=BrowseGroupDisplayMode.AUTO_COLLAPSE,
                ),
            )

    def test_invalid_font_size_falls_back_without_losing_other_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "recent_projects": [],
                        "display": {
                            "segment_density": "wrapped",
                            "workspace_mode": "browse",
                        },
                    }
                ),
                encoding="utf-8",
            )

            legacy = WorkspaceStateRepository(config_dir).display_preferences()

            self.assertIs(legacy.segment_density, SegmentDensity.WRAPPED)
            self.assertIs(legacy.workspace_mode, WorkspaceMode.BROWSE)
            self.assertEqual(legacy.editor_font_size, DEFAULT_EDITOR_FONT_SIZE)

        invalid_values = (True, "18", 9, 29)
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp_dir:
                config_dir = Path(temp_dir) / "app-data"
                config_dir.mkdir()
                state_path = config_dir / "workspace.json"
                payload = {
                    "schema_version": 1,
                    "recent_projects": [],
                    "display": {
                        "segment_density": "wrapped",
                        "workspace_mode": "browse",
                        "editor_font_size": invalid,
                    },
                }
                rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                state_path.write_text(rendered, encoding="utf-8")

                with self.assertLogs("workspace_state", level="WARNING"):
                    preferences = WorkspaceStateRepository(
                        config_dir
                    ).display_preferences()

                self.assertIs(preferences.segment_density, SegmentDensity.WRAPPED)
                self.assertIs(preferences.workspace_mode, WorkspaceMode.BROWSE)
                self.assertEqual(
                    preferences.editor_font_size,
                    DEFAULT_EDITOR_FONT_SIZE,
                )
                self.assertEqual(state_path.read_text(encoding="utf-8"), rendered)

    def test_font_size_write_failure_preserves_previous_file_and_local_isolation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            project_path = root / "project.json"
            tm_path = root / "memory.jsonl"
            terms_path = root / "terms.csv"
            project_path.write_text('{"segments":[]}\n', encoding="utf-8")
            tm_path.write_text('{"source":"One","target":"一"}\n', encoding="utf-8")
            terms_path.write_text("Source,Target\nOne,一\n", encoding="utf-8")
            external_snapshots = {
                path: path.read_bytes()
                for path in (project_path, tm_path, terms_path)
            }

            repository = WorkspaceStateRepository(config_dir)
            previous = DisplayPreferences(editor_font_size=18)
            repository.update_display_preferences(previous)
            state_snapshot = repository.state_path.read_bytes()

            with (
                mock.patch("workspace_state.os.replace", side_effect=OSError("disk full")),
                self.assertRaises(WorkspaceStateError),
            ):
                repository.update_display_preferences(
                    dataclasses.replace(previous, editor_font_size=19)
                )

            self.assertEqual(repository.display_preferences(), previous)
            self.assertEqual(repository.state_path.read_bytes(), state_snapshot)
            for path, snapshot in external_snapshots.items():
                self.assertEqual(path.read_bytes(), snapshot)

    def test_preprocess_preferences_round_trip_order_and_defensive_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            repository = WorkspaceStateRepository(config_dir)
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

            returned = repository.update_preprocess_preferences(preferences)
            restored = WorkspaceStateRepository(config_dir)
            payload = json.loads(repository.state_path.read_text(encoding="utf-8"))

            self.assertEqual(returned, preferences)
            self.assertIsNot(returned, preferences)
            self.assertIsNot(returned.rules[0], preferences.rules[0])
            self.assertEqual(restored.preprocess_preferences(), preferences)
            self.assertEqual(
                payload["preprocessing"],
                {
                    "rules": [
                        {
                            "find": "first",
                            "replacement": "one",
                            "enabled": False,
                        },
                        {
                            "find": "second",
                            "replacement": "two",
                            "enabled": True,
                        },
                    ],
                    "include_draft": False,
                    "include_confirmed": True,
                },
            )

            object.__setattr__(returned.rules[0], "find", "tampered")
            reread = repository.preprocess_preferences()
            self.assertEqual(reread.rules[0].find, "first")
            self.assertIsNot(reread.rules[0], repository.preprocess_preferences().rules[0])

    def test_missing_or_invalid_preprocess_member_uses_complete_default(self) -> None:
        invalid_members: tuple[object, ...] = (
            [],
            {"rules": [], "include_draft": False},
            {
                "rules": [],
                "include_draft": False,
                "include_confirmed": False,
            },
            {
                "rules": [
                    {"find": "", "replacement": "x", "enabled": True}
                ],
                "include_draft": True,
                "include_confirmed": False,
            },
            {
                "rules": [
                    {"find": "x", "replacement": "y", "enabled": 1}
                ],
                "include_draft": True,
                "include_confirmed": False,
            },
        )
        for invalid in invalid_members:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp_dir:
                config_dir = Path(temp_dir) / "app-data"
                config_dir.mkdir()
                state_path = config_dir / "workspace.json"
                payload = {
                    "schema_version": 1,
                    "recent_projects": [],
                    "display": {
                        "segment_density": "wrapped",
                        "workspace_mode": "browse",
                        "editor_font_size": 20,
                    },
                    "preprocessing": invalid,
                }
                rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
                state_path.write_text(rendered, encoding="utf-8")

                with self.assertLogs("workspace_state", level="WARNING"):
                    repository = WorkspaceStateRepository(config_dir)

                self.assertEqual(
                    repository.preprocess_preferences(),
                    PreprocessPreferences(),
                )
                self.assertEqual(
                    repository.display_preferences(),
                    DisplayPreferences(
                        segment_density=SegmentDensity.WRAPPED,
                        workspace_mode=WorkspaceMode.BROWSE,
                        editor_font_size=20,
                    ),
                )
                self.assertEqual(state_path.read_text(encoding="utf-8"), rendered)

        with tempfile.TemporaryDirectory() as temp_dir:
            repository = WorkspaceStateRepository(Path(temp_dir) / "app-data")
            self.assertEqual(
                repository.preprocess_preferences(),
                PreprocessPreferences(),
            )

    def test_preprocess_write_failure_preserves_file_and_last_known_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            repository = WorkspaceStateRepository(config_dir)
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
            repository.update_preprocess_preferences(previous)
            state_snapshot = repository.state_path.read_bytes()
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
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(WorkspaceStateError),
            ):
                repository.update_preprocess_preferences(candidate)

            self.assertEqual(repository.preprocess_preferences(), previous)
            self.assertEqual(repository.state_path.read_bytes(), state_snapshot)
            self.assertEqual(
                WorkspaceStateRepository(config_dir).preprocess_preferences(),
                previous,
            )


if __name__ == "__main__":
    unittest.main()
