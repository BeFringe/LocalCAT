from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMSuggestion,
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
            tm = TMSuggestion(
                source="The office is ready.",
                target="办公室准备就绪。",
                resource_id="tm",
                resource_name="TM",
            )
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

    def test_add_term_requires_an_active_update_termbase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller, repository = self._session(Path(temp_dir))
            terms = next(
                resource
                for resource in repository.list_resources()
                if resource.kind is ResourceKind.TERMBASE
            )
            repository.update_resource(replace(terms, update=False))

            with self.assertRaises(EditorControllerError):
                controller.add_term("office", "办公室")
            with self.assertRaises(EditorControllerError):
                controller.add_term("", "办公室")


if __name__ == "__main__":
    unittest.main()
