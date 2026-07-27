from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ImportRequest,
    ResourceKind,
)
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository


class EditorControllerResourcesTest(unittest.TestCase):
    def _controller(self, root: Path) -> EditorController:
        controller = EditorController(ResourceRepository(root / "app-data"))
        controller.set_project(
            EditorProject(
                name="Resources",
                segments=(EditorSegment(id="1", source="The office is ready."),),
            )
        )
        return controller

    def _write_tmx(self, path: Path) -> None:
        path.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
            <tmx version="1.4"><header srclang="en-US"/><body>
              <tu><tuv xml:lang="en-US"><seg>The office is ready.</seg></tuv>
                  <tuv xml:lang="zh-CN"><seg>办公室准备好了。</seg></tuv></tu>
            </body></tmx>
            """,
            encoding="utf-8",
        )

    def test_create_and_update_resource_through_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = self._controller(Path(temp_dir))
            resource = controller.create_resource("Client TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                json.dumps(
                    {"source": "The office is ready.", "target": "办公室已就绪。"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            controller.reload_resources()
            visible = controller.suggestions()

            changed = controller.update_resource(replace(resource, lookup=False))
            hidden = controller.suggestions()
            restored_repository = ResourceRepository(controller.repository.config_dir)

        self.assertEqual(len(visible.tm_matches), 1)
        self.assertEqual(hidden.tm_matches, ())
        self.assertFalse(changed.lookup)
        self.assertFalse(restored_repository.get(resource.id).lookup)

    def test_tmx_import_hot_reloads_current_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = self._controller(root)
            resource = controller.create_resource("Imported TM", ResourceKind.TRANSLATION_MEMORY)
            source = root / "memory.tmx"
            self._write_tmx(source)

            before = controller.suggestions()
            report = controller.import_resource(
                ImportRequest(
                    resource_id=resource.id,
                    input_path=source.resolve(),
                    source_locale="en-US",
                    target_locale="zh-CN",
                )
            )
            after = controller.suggestions()

        self.assertEqual(before.tm_matches, ())
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.errors, ())
        self.assertEqual(after.tm_matches[0].target, "办公室准备好了。")

    def test_termbase_import_hot_reloads_current_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = self._controller(root)
            resource = controller.create_resource("Imported terms", ResourceKind.TERMBASE)
            source = root / "terms.csv"
            source.write_text("Source,Target\noffice,办公室\n", encoding="utf-8-sig")

            report = controller.import_resource(
                ImportRequest(resource_id=resource.id, input_path=source.resolve())
            )
            suggestions = controller.suggestions()

        self.assertEqual(report.imported, 1)
        self.assertEqual(len(suggestions.terms), 1)
        self.assertEqual(suggestions.terms[0].resource_id, resource.id)

    def test_failed_import_keeps_previous_engine_and_resource_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = self._controller(root)
            resource = controller.create_resource("Stable TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                json.dumps(
                    {"source": "The office is ready.", "target": "稳定译文"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            controller.reload_resources()
            original = resource.path.read_bytes()
            unsafe = root / "unsafe.tmx"
            unsafe.write_text(
                '<!DOCTYPE tmx SYSTEM "tmx14.dtd"><tmx><body/></tmx>',
                encoding="utf-8",
            )

            report = controller.import_resource(
                ImportRequest(
                    resource_id=resource.id,
                    input_path=unsafe.resolve(),
                    source_locale="en-US",
                    target_locale="zh-CN",
                )
            )
            suggestions = controller.suggestions()
            resource_bytes = resource.path.read_bytes()

        self.assertTrue(report.errors)
        self.assertEqual(resource_bytes, original)
        self.assertEqual(suggestions.tm_matches[0].target, "稳定译文")

    def test_reload_failure_retains_previous_engine_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = self._controller(root)
            resource = controller.create_resource("Stable TM", ResourceKind.TRANSLATION_MEMORY)
            resource.path.write_text(
                json.dumps(
                    {"source": "The office is ready.", "target": "缓存译文"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            controller.reload_resources()
            resource.path.write_text("{not-json\n", encoding="utf-8")

            with self.assertRaises(EditorControllerError):
                controller.reload_resources()
            suggestions = controller.suggestions()

        self.assertEqual(suggestions.tm_matches[0].target, "缓存译文")

    def test_import_rejects_unknown_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            controller = self._controller(root)
            source = root / "terms.csv"
            source.write_text("office,办公室\n", encoding="utf-8-sig")

            with self.assertRaises(EditorControllerError):
                controller.import_resource(
                    ImportRequest(resource_id="missing", input_path=source.resolve())
                )


if __name__ == "__main__":
    unittest.main()
