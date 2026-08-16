from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from editor_contracts import EditorProject, EditorSegment, ResourceKind
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository


class EditorControllerSessionTest(unittest.TestCase):
    def _controller(self, root: Path) -> tuple[EditorController, ResourceRepository]:
        repository = ResourceRepository(root / "app-data")
        tm_active = repository.create_resource("Primary TM", ResourceKind.TRANSLATION_MEMORY)
        tm_active.path.write_text(
            json.dumps({"source": "The office is ready.", "target": "办公室准备好了。"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        tm_inactive = repository.create_resource("Inactive TM", ResourceKind.TRANSLATION_MEMORY)
        tm_inactive.path.write_text(
            json.dumps({"source": "The office is ready.", "target": "不应出现"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        repository.update_resource(replace(tm_inactive, active=False))

        terms_active = repository.create_resource("Primary terms", ResourceKind.TERMBASE)
        terms_active.path.write_text("office,办公室\n", encoding="utf-8-sig")
        terms_disabled = repository.create_resource("Hidden terms", ResourceKind.TERMBASE)
        terms_disabled.path.write_text("ready,就绪\n", encoding="utf-8-sig")
        repository.update_resource(replace(terms_disabled, lookup=False))
        return EditorController(repository), repository

    def test_load_sample_and_open_project_reset_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller, _ = self._controller(root)
            sample = controller.load_sample()
            project_path = root / "project.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Opened",
                        "segments": [
                            {"id": "one", "source": "First", "target": "第一", "confirmed": True},
                            {"id": "two", "source": "Second"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            opened = controller.open_project(project_path)

        self.assertEqual(len(sample.segments), 3)
        self.assertEqual(opened.name, "Opened")
        self.assertEqual(controller.current_index, 0)
        self.assertFalse(controller.dirty)
        self.assertEqual(controller.confirmed_count, 1)
        self.assertEqual(controller.completion_ratio, 0.5)

    def test_editing_confirmed_segment_restores_unconfirmed_and_navigation_keeps_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _ = self._controller(Path(temp_dir))
            project = EditorProject(
                name="Navigation",
                segments=(
                    EditorSegment(id="1", source="One", target="一", confirmed=True),
                    EditorSegment(id="2", source="Two"),
                    EditorSegment(id="3", source="Three", target="三", confirmed=True),
                    EditorSegment(id="4", source="Four"),
                ),
            )
            controller.set_project(project)

            changed = controller.update_target("更新的一")
            controller.move(1)
            controller.update_target("二")
            controller.move(1, unconfirmed_only=True)

        self.assertFalse(changed.segments[0].confirmed)
        self.assertTrue(controller.dirty)
        self.assertEqual(controller.current_index, 3)
        self.assertEqual(controller.project.segments[0].target, "更新的一")
        self.assertEqual(controller.project.segments[1].target, "二")
        self.assertEqual(controller.confirmed_count, 1)

    def test_navigation_clamps_and_requires_a_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _ = self._controller(Path(temp_dir))
            with self.assertRaises(EditorControllerError):
                controller.move(1)
            controller.set_project(
                EditorProject(name="One", segments=(EditorSegment(id="1", source="Only"),))
            )
            controller.move(-1)
            controller.move(1)
            self.assertEqual(controller.current_index, 0)
            with self.assertRaises(EditorControllerError):
                controller.move(0)

    def test_suggestions_are_parallel_provenanced_and_follow_lookup_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._controller(Path(temp_dir))
            controller.set_project(
                EditorProject(
                    name="Suggestions",
                    segments=(EditorSegment(id="1", source="The office is ready."),),
                )
            )

            suggestions = controller.suggestions()
            primary_tm = next(
                resource for resource in repository.list_resources() if resource.name == "Primary TM"
            )
            repository.update_resource(replace(primary_tm, lookup=False))
            filtered = controller.suggestions()

        self.assertEqual(len(suggestions.tm_matches), 1)
        self.assertEqual(suggestions.tm_matches[0].resource_name, "Primary TM")
        self.assertEqual(suggestions.tm_matches[0].target, "办公室准备好了。")
        self.assertEqual(len(suggestions.terms), 1)
        self.assertEqual(suggestions.terms[0].source_term, "office")
        self.assertEqual(suggestions.terms[0].resource_name, "Primary terms")
        self.assertEqual(filtered.tm_matches, ())
        self.assertEqual(len(filtered.terms), 1)

    def test_no_match_is_a_structured_empty_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _ = self._controller(Path(temp_dir))
            controller.set_project(
                EditorProject(name="Empty", segments=(EditorSegment(id="1", source="Nothing"),))
            )

            suggestions = controller.suggestions()

        self.assertEqual(suggestions.tm_matches, ())
        self.assertEqual(suggestions.terms, ())

    def test_reopens_project_at_stable_segment_and_can_exit_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "app-data"
            project_path = root / "project.json"
            project_path.write_text(
                json.dumps(
                    {
                        "name": "Resume",
                        "segments": [
                            {"id": "a", "source": "Alpha"},
                            {"id": "b", "source": "Beta"},
                            {"id": "c", "source": "Gamma"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first = EditorController(ResourceRepository(config_dir))
            first.open_project(project_path)
            first.go_to(1)

            project_path.write_text(
                json.dumps(
                    {
                        "name": "Resume",
                        "segments": [
                            {"id": "a", "source": "Alpha"},
                            {"id": "c", "source": "Gamma"},
                            {"id": "b", "source": "Beta"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            restored = EditorController(ResourceRepository(config_dir))
            restored.open_project(project_path)

            self.assertEqual(restored.current_index, 2)
            self.assertEqual(restored.current_segment.id, "b")
            self.assertEqual(restored.recent_projects()[0].path, project_path.resolve())

            restored.close_project()
            self.assertFalse(restored.has_project)
            self.assertEqual(restored.current_index, 0)


if __name__ == "__main__":
    unittest.main()
