from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from editor_contracts import DisplayPreferences, SegmentDensity, WorkspaceMode
from workspace_state import WorkspaceStateRepository


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
            )
            repository.update_display_preferences(preferences)
            restored = WorkspaceStateRepository(config_dir)
            payload = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(restored.display_preferences(), preferences)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["display"]["segment_density"], "wrapped")
            self.assertEqual(payload["display"]["workspace_mode"], "browse")


if __name__ == "__main__":
    unittest.main()
