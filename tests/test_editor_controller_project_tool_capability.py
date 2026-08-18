from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from editor_contracts import EditorProject, EditorSegment, ProjectToolCapability
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository


class EditorControllerProjectToolCapabilityTests(unittest.TestCase):
    def _controller(self, root: Path) -> EditorController:
        return EditorController(ResourceRepository(root / "app-data"))

    def _write_json_project(self, path: Path, *, source: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "name": path.stem,
                    "segments": [{"id": "segment-1", "source": source}],
                }
            ),
            encoding="utf-8",
        )

    def test_no_project_and_sample_are_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = self._controller(Path(temporary))

            no_project = controller.project_tool_capability()
            controller.load_sample()
            sample = controller.project_tool_capability()

        self.assertIs(type(no_project), ProjectToolCapability)
        self.assertEqual(
            no_project,
            ProjectToolCapability(
                project_session_id=None,
                single_json_tools_available=False,
                project_kind="none",
                unavailable_reason="PROJECT_TOOLS.NO_PROJECT",
            ),
        )
        self.assertEqual(sample.project_session_id, controller.project_session_id)
        self.assertFalse(sample.single_json_tools_available)
        self.assertEqual(sample.project_kind, "sample")
        self.assertEqual(
            sample.unavailable_reason,
            "PROJECT_TOOLS.JSON_REQUIRED",
        )

    def test_json_suffix_is_case_insensitive_and_reuses_tm_session_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            lower_path = root / "lower.json"
            mixed_path = root / "mixed.JsOn"
            self._write_json_project(lower_path, source="Lower")
            self._write_json_project(mixed_path, source="Mixed")

            controller.open_project(lower_path)
            lower = controller.project_tool_capability()
            lower_session = controller.project_session_id
            controller.open_project(mixed_path)
            mixed = controller.project_tool_capability()

        self.assertTrue(lower.single_json_tools_available)
        self.assertEqual(lower.project_kind, "json")
        self.assertIsNone(lower.unavailable_reason)
        self.assertEqual(lower.project_session_id, lower_session)
        self.assertNotEqual(mixed.project_session_id, lower_session)
        self.assertEqual(mixed.project_session_id, controller.project_session_id)
        self.assertTrue(mixed.single_json_tools_available)
        self.assertEqual(mixed.project_kind, "json")
        self.assertIsNone(mixed.unavailable_reason)

    def test_txt_and_pathless_install_remain_open_but_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            text_path = root / "notes.TXT"
            text_path.write_text("One line\n", encoding="utf-8")

            controller.open_project(text_path)
            text = controller.project_tool_capability()
            text_session = controller.project_session_id
            controller.set_project(
                EditorProject(
                    name="Installed without a path",
                    segments=(EditorSegment(id="one", source="Source"),),
                )
            )
            pathless = controller.project_tool_capability()

        self.assertTrue(controller.has_project)
        self.assertFalse(text.single_json_tools_available)
        self.assertEqual(text.project_kind, "txt")
        self.assertEqual(text.project_session_id, text_session)
        self.assertEqual(text.unavailable_reason, "PROJECT_TOOLS.JSON_REQUIRED")
        self.assertFalse(pathless.single_json_tools_available)
        self.assertEqual(pathless.project_kind, "sample")
        self.assertEqual(pathless.project_session_id, controller.project_session_id)
        self.assertNotEqual(pathless.project_session_id, text_session)
        self.assertEqual(
            pathless.unavailable_reason,
            "PROJECT_TOOLS.JSON_REQUIRED",
        )

    def test_close_rotates_the_shared_session_and_returns_no_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            project_path = root / "project.json"
            self._write_json_project(project_path, source="Source")
            controller.open_project(project_path)
            opened_session = controller.project_session_id

            controller.close_project()
            closed_session = controller.project_session_id
            capability = controller.project_tool_capability()

        self.assertNotEqual(closed_session, opened_session)
        self.assertEqual(
            capability,
            ProjectToolCapability(
                project_session_id=None,
                single_json_tools_available=False,
                project_kind="none",
                unavailable_reason="PROJECT_TOOLS.NO_PROJECT",
            ),
        )

    def test_failed_open_is_body_free_and_preserves_the_entire_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            good_path = root / "good.json"
            invalid_path = root / "private-body.json"
            self._write_json_project(good_path, source="Stable source")
            invalid_path.write_text('{"segments":[{"source":', encoding="utf-8")
            controller.open_project(good_path)
            controller.update_target("Unsaved target")
            before_project = controller.project
            before_index = controller.current_index
            before_dirty = controller.dirty
            before_session = controller.project_session_id
            before_epoch = controller.query_epoch
            before_capability = controller.project_tool_capability()

            with self.assertRaisesRegex(
                EditorControllerError,
                "^PROJECT\\.LOAD_FAILED$",
            ):
                controller.open_project(invalid_path)

            after_capability = controller.project_tool_capability()

        self.assertIs(controller.project, before_project)
        self.assertEqual(controller.current_index, before_index)
        self.assertEqual(controller.dirty, before_dirty)
        self.assertEqual(controller.project_session_id, before_session)
        self.assertEqual(controller.query_epoch, before_epoch)
        self.assertEqual(after_capability, before_capability)

    def test_failed_save_is_body_free_and_preserves_the_entire_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            project_path = root / "good.json"
            self._write_json_project(project_path, source="Stable source")
            controller.open_project(project_path)
            controller.update_target("Unsaved target")
            before_project = controller.project
            before_session = controller.project_session_id
            before_epoch = controller.query_epoch
            before_capability = controller.project_tool_capability()

            with self.assertRaisesRegex(
                EditorControllerError,
                "^PROJECT\\.SAVE_FAILED$",
            ):
                controller.save_project(root / "body-must-not-leak.txt")

            after_capability = controller.project_tool_capability()

        self.assertIs(controller.project, before_project)
        self.assertTrue(controller.dirty)
        self.assertEqual(controller.project_session_id, before_session)
        self.assertEqual(controller.query_epoch, before_epoch)
        self.assertEqual(after_capability, before_capability)

    def test_capability_is_a_fresh_defensive_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = self._controller(root)
            project_path = root / "project.json"
            self._write_json_project(project_path, source="Source")
            controller.open_project(project_path)

            returned = controller.project_tool_capability()
            object.__setattr__(returned, "project_session_id", "tampered")
            object.__setattr__(returned, "single_json_tools_available", False)
            object.__setattr__(returned, "project_kind", "txt")
            object.__setattr__(
                returned,
                "unavailable_reason",
                "PROJECT_TOOLS.JSON_REQUIRED",
            )
            fresh = controller.project_tool_capability()

        self.assertIs(type(fresh), ProjectToolCapability)
        self.assertEqual(fresh.project_session_id, controller.project_session_id)
        self.assertTrue(fresh.single_json_tools_available)
        self.assertEqual(fresh.project_kind, "json")
        self.assertIsNone(fresh.unavailable_reason)


if __name__ == "__main__":
    unittest.main()
