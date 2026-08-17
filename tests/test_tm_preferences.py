from __future__ import annotations

import dataclasses
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from editor_contracts import TMPreferences
from workspace_state import WorkspaceStateError, WorkspaceStateRepository


class _FloatSubclass(float):
    pass


class _IntSubclass(int):
    pass


class TMPreferencesContractTest(unittest.TestCase):
    def test_defaults_boundaries_and_frozen_shape(self) -> None:
        default = TMPreferences()
        lower = TMPreferences(minimum_similarity=0.60)
        upper = TMPreferences(minimum_similarity=1.00)

        self.assertEqual(default, lower)
        self.assertEqual(default.minimum_similarity, 0.60)
        self.assertEqual(default.result_limit, 10)
        self.assertEqual(upper.minimum_similarity, 1.00)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            default.minimum_similarity = 0.75  # pyright: ignore[reportAttributeAccessIssue]

    def test_minimum_similarity_requires_an_exact_finite_float_in_range(self) -> None:
        invalid_values: tuple[object, ...] = (
            True,
            1,
            "0.75",
            _FloatSubclass(0.75),
            math.nan,
            math.inf,
            -math.inf,
            0.5999999999999999,
            1.0000000000000002,
        )

        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(
                (TypeError, ValueError)
            ):
                TMPreferences(
                    minimum_similarity=invalid  # pyright: ignore[reportArgumentType]
                )

    def test_result_limit_is_the_exact_fixed_integer_ten(self) -> None:
        invalid_values: tuple[object, ...] = (
            True,
            10.0,
            _IntSubclass(10),
            9,
            11,
        )

        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(
                (TypeError, ValueError)
            ):
                TMPreferences(result_limit=invalid)  # pyright: ignore[reportArgumentType]


class TMPreferencesRepositoryTest(unittest.TestCase):
    def test_default_and_valid_value_round_trip_in_workspace_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            project_path = root / "project.json"
            tm_path = root / "memory.jsonl"
            termbase_path = root / "terms.csv"
            project_path.write_text('{"segments":[]}\n', encoding="utf-8")
            tm_path.write_text('{"source":"One","target":"一"}\n', encoding="utf-8")
            termbase_path.write_text("Source,Target\nOne,一\n", encoding="utf-8")
            external_snapshots = {
                path: path.read_bytes()
                for path in (project_path, tm_path, termbase_path)
            }

            repository = WorkspaceStateRepository(config_dir)
            self.assertEqual(repository.tm_preferences(), TMPreferences())
            saved = repository.update_tm_preferences(
                TMPreferences(minimum_similarity=0.75)
            )
            restored = WorkspaceStateRepository(config_dir)
            payload = json.loads(repository.state_path.read_text(encoding="utf-8"))

            self.assertEqual(saved, TMPreferences(minimum_similarity=0.75))
            self.assertEqual(restored.tm_preferences(), saved)
            self.assertEqual(
                payload["tm_preferences"],
                {"minimum_similarity": 0.75},
            )
            self.assertNotIn("result_limit", payload["tm_preferences"])
            self.assertEqual(
                {path.relative_to(root) for path in root.rglob("*") if path.is_file()},
                {
                    Path("app-data/workspace.json"),
                    Path("project.json"),
                    Path("memory.jsonl"),
                    Path("terms.csv"),
                },
            )
            for path, snapshot in external_snapshots.items():
                self.assertEqual(path.read_bytes(), snapshot)

    def test_preference_survives_restart_and_project_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = WorkspaceStateRepository(root / "app-data")
            expected = repository.update_tm_preferences(
                TMPreferences(minimum_similarity=0.83)
            )

            for index in range(2):
                path = root / f"project-{index}.json"
                path.write_text('{"segments":[]}\n', encoding="utf-8")
                repository.remember_project(path, f"segment-{index}", index)
                self.assertEqual(repository.tm_preferences(), expected)

            restored = WorkspaceStateRepository(root / "app-data")
            self.assertEqual(restored.tm_preferences(), expected)
            self.assertEqual(len(restored.recent_projects()), 2)

    def test_missing_invalid_and_old_version_state_fall_back_without_rewrite(self) -> None:
        invalid_tm_values: tuple[object, ...] = (
            True,
            1,
            "0.75",
            math.nan,
            math.inf,
            0.59,
            1.01,
        )
        missing_payload = {
            "schema_version": 1,
            "recent_projects": [],
            "display": {},
        }
        payloads: list[dict[str, object]] = [
            {
                "schema_version": 1,
                "recent_projects": [],
                "display": {},
                "tm_preferences": [],
            },
            {
                "schema_version": 0,
                "recent_projects": [],
                "display": {},
                "tm_preferences": {"minimum_similarity": 0.90},
            },
        ]
        payloads.extend(
            {
                "schema_version": 1,
                "recent_projects": [],
                "display": {},
                "tm_preferences": {"minimum_similarity": invalid},
            }
            for invalid in invalid_tm_values
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            rendered = json.dumps(missing_payload) + "\n"
            state_path.write_text(rendered, encoding="utf-8")

            preferences = WorkspaceStateRepository(config_dir).tm_preferences()

            self.assertEqual(preferences, TMPreferences())
            self.assertEqual(state_path.read_text(encoding="utf-8"), rendered)

        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp_dir:
                config_dir = Path(temp_dir) / "app-data"
                config_dir.mkdir()
                state_path = config_dir / "workspace.json"
                rendered = json.dumps(payload, allow_nan=True) + "\n"
                state_path.write_text(rendered, encoding="utf-8")

                with self.assertLogs("workspace_state", level="WARNING"):
                    preferences = WorkspaceStateRepository(
                        config_dir
                    ).tm_preferences()

                self.assertEqual(preferences, TMPreferences())
                self.assertEqual(state_path.read_text(encoding="utf-8"), rendered)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            config_dir.mkdir()
            state_path = config_dir / "workspace.json"
            state_path.write_text("{not-json", encoding="utf-8")

            with self.assertLogs("workspace_state", level="WARNING"):
                repository = WorkspaceStateRepository(config_dir)

            self.assertEqual(repository.tm_preferences(), TMPreferences())
            self.assertEqual(state_path.read_text(encoding="utf-8"), "{not-json")

    def test_invalid_update_preserves_previous_value_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = WorkspaceStateRepository(Path(temp_dir) / "app-data")
            previous = repository.update_tm_preferences(
                TMPreferences(minimum_similarity=0.72)
            )
            state_snapshot = repository.state_path.read_bytes()
            invalid = object.__new__(TMPreferences)
            object.__setattr__(invalid, "minimum_similarity", 0.50)
            object.__setattr__(invalid, "result_limit", 10)

            for value in (object(), invalid):
                with self.subTest(value=value), self.assertRaises(
                    WorkspaceStateError
                ):
                    repository.update_tm_preferences(
                        value  # pyright: ignore[reportArgumentType]
                    )

                self.assertEqual(repository.tm_preferences(), previous)
                self.assertEqual(repository.state_path.read_bytes(), state_snapshot)

    def test_atomic_write_failure_leaves_no_new_value_or_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            repository = WorkspaceStateRepository(config_dir)

            with (
                mock.patch("workspace_state.os.replace", side_effect=OSError("disk full")),
                self.assertRaises(WorkspaceStateError),
            ):
                repository.update_tm_preferences(
                    TMPreferences(minimum_similarity=0.77)
                )

            self.assertEqual(repository.tm_preferences(), TMPreferences())
            self.assertFalse(repository.state_path.exists())
            self.assertEqual(tuple(config_dir.iterdir()), ())

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "app-data"
            repository = WorkspaceStateRepository(config_dir)
            previous = repository.update_tm_preferences(
                TMPreferences(minimum_similarity=0.70)
            )
            state_snapshot = repository.state_path.read_bytes()

            with (
                mock.patch("workspace_state.os.replace", side_effect=OSError("disk full")),
                self.assertRaises(WorkspaceStateError),
            ):
                repository.update_tm_preferences(
                    TMPreferences(minimum_similarity=0.88)
                )

            self.assertEqual(repository.tm_preferences(), previous)
            self.assertEqual(repository.state_path.read_bytes(), state_snapshot)
            self.assertEqual(
                tuple(path.name for path in config_dir.iterdir()),
                ("workspace.json",),
            )

if __name__ == "__main__":
    unittest.main()
