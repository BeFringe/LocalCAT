from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TermSuggestion,
)
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository
from tm_engine import TMEngine


class EditorControllerWritesTest(unittest.TestCase):
    def _session(
        self,
        root: Path,
        *,
        second_tm: bool = False,
    ) -> tuple[EditorController, ResourceRepository]:
        repository = ResourceRepository(root / "app-data")
        repository.create_resource("Writable TM", ResourceKind.TRANSLATION_MEMORY)
        if second_tm:
            repository.create_resource("Second TM", ResourceKind.TRANSLATION_MEMORY)
        repository.create_resource("Writable terms", ResourceKind.TERMBASE)
        controller = EditorController(repository)
        controller.set_project(
            EditorProject(
                name="Writes",
                segments=(
                    EditorSegment(id="1", source="The office is ready.", target="办公室准备好了。"),
                    EditorSegment(id="2", source="Already", target="已有", confirmed=True),
                    EditorSegment(id="3", source="Another office"),
                ),
            )
        )
        return controller, repository

    def test_confirm_writes_tm_marks_segment_and_moves_to_next_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._session(Path(temp_dir))

            result = controller.confirm_current()
            tm_resource = next(
                resource
                for resource in repository.list_resources()
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
            )
            persisted = TMEngine(str(tm_resource.path)).query_exact("The office is ready.")

        self.assertTrue(result.project.segments[0].confirmed)
        self.assertEqual(result.current_index, 2)
        self.assertEqual(controller.current_index, 2)
        self.assertEqual(result.write_report.written_resource_ids, (tm_resource.id,))
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.target, "办公室准备好了。")

    def test_confirm_failure_keeps_segment_unconfirmed_and_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._session(Path(temp_dir), second_tm=True)
            tm_resources = [
                resource
                for resource in repository.list_resources()
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
            ]
            failing = tm_resources[1]
            failing.path.unlink()
            failing.path.mkdir()

            with redirect_stdout(io.StringIO()):
                result = controller.confirm_current()

        self.assertFalse(result.project.segments[0].confirmed)
        self.assertEqual(result.current_index, 0)
        self.assertEqual(controller.current_index, 0)
        self.assertEqual(result.write_report.written_resource_ids, (tm_resources[0].id,))
        self.assertTrue(result.write_report.errors)

    def test_confirm_rejects_empty_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _ = self._session(Path(temp_dir))
            controller.update_target("  ")

            with self.assertRaises(EditorControllerError):
                controller.confirm_current()

        self.assertFalse(controller.project.segments[0].confirmed)
        self.assertEqual(controller.current_index, 0)

    def test_apply_tm_and_insert_term_never_auto_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, _ = self._session(Path(temp_dir))
            controller.update_target("")
            tm_resource = next(
                resource
                for resource in controller.list_resources()
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
            )
            tm_resource.path.write_text(
                json.dumps(
                    {
                        "source": "The office is ready.",
                        "target": "办公室准备就绪。",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            controller.reload_resources()
            tm = controller.suggestions().tm_matches[0]
            term = TermSuggestion(
                source_term="office",
                target_term="办公室",
                start_index=4,
                end_index=10,
                resource_id="terms",
                resource_name="Terms",
            )

            controller.apply_tm_suggestion(tm)
            controller.insert_term_suggestion(term, position=3)
            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(replace(tm, source="A stale segment"))

        self.assertEqual(controller.current_segment.target, "办公室办公室准备就绪。")
        self.assertFalse(controller.current_segment.confirmed)

    def test_add_term_persists_and_refreshes_current_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._session(Path(temp_dir))

            resource = controller.add_term("office", "办公室")
            suggestions = controller.suggestions()
            reloaded = EditorController(ResourceRepository(repository.config_dir))
            reloaded.set_project(controller.project)
            restored = reloaded.suggestions()

        self.assertEqual(resource.kind, ResourceKind.TERMBASE)
        self.assertEqual(len(suggestions.terms), 1)
        self.assertEqual(suggestions.terms[0].target_term, "办公室")
        self.assertEqual(len(restored.terms), 1)

    def test_add_term_update_disabled_is_actionable_zero_write_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._session(Path(temp_dir))
            controller.create_resource("Second writable terms", ResourceKind.TERMBASE)
            termbases = tuple(
                resource
                for resource in repository.list_resources()
                if resource.kind is ResourceKind.TERMBASE
            )
            disabled = tuple(
                controller.update_resource(replace(resource, update=False))
                for resource in termbases
            )
            resource_bytes = {
                resource.path: resource.path.read_bytes()
                for resource in repository.list_resources()
            }
            registry_bytes = repository.registry_path.read_bytes()
            resources_before = repository.list_resources()
            project_before = controller.project

            with self.assertRaisesRegex(
                EditorControllerError,
                r"语言资源设置.*术语表.*Active.*Update",
            ):
                controller.add_term("office", "办公室")
            self.assertEqual(repository.registry_path.read_bytes(), registry_bytes)
            self.assertEqual(repository.list_resources(), resources_before)
            self.assertEqual(controller.project, project_before)
            for path, expected in resource_bytes.items():
                self.assertEqual(path.read_bytes(), expected)

            with self.assertRaisesRegex(EditorControllerError, r"不能为空"):
                controller.add_term("", "办公室")
            self.assertEqual(repository.registry_path.read_bytes(), registry_bytes)
            for path, expected in resource_bytes.items():
                self.assertEqual(path.read_bytes(), expected)

            enabled = controller.update_resource(replace(disabled[1], update=True))
            written = controller.add_term("office", "办公室")
            suggestions = controller.suggestions()

        self.assertEqual(written.id, enabled.id)
        self.assertEqual(
            [(term.source_term, term.target_term) for term in suggestions.terms],
            [("office", "办公室")],
        )

    def test_add_term_without_any_termbase_is_actionable_and_zero_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = ResourceRepository(root / "app-data")
            tm = repository.create_resource(
                "Only TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            controller = EditorController(repository)
            controller.set_project(
                EditorProject(
                    name="No termbase",
                    segments=(EditorSegment(id="1", source="office"),),
                )
            )
            registry_bytes = repository.registry_path.read_bytes()
            tm_bytes = tm.path.read_bytes()
            resources_before = repository.list_resources()
            project_before = controller.project

            with self.assertRaisesRegex(
                EditorControllerError,
                r"语言资源设置.*术语表.*Active.*Update",
            ):
                controller.add_term("office", "办公室")

            self.assertEqual(repository.registry_path.read_bytes(), registry_bytes)
            self.assertEqual(tm.path.read_bytes(), tm_bytes)
            self.assertEqual(repository.list_resources(), resources_before)
            self.assertEqual(controller.project, project_before)


if __name__ == "__main__":
    unittest.main()
