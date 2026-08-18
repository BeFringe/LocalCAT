"""Task 5.3 structured TM write coordination tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceConfig,
    WriteReport,
)
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tests.test_editor_tm_adapter_append import (
    _RecordingLegacyBackend,
    _RecordingStore,
    _adapter,
    _binding,
    _canonical_binding,
    _config,
    _digest,
)
from tm_application_composition import RuntimeOpenBinding


def _controller(
    root: Path,
    configs: tuple[ResourceConfig, ...],
    bindings: dict[Path, RuntimeOpenBinding],
) -> tuple[EditorController, EditorTMAdapter]:
    adapter, _runtime = _adapter(configs, bindings)
    controller = EditorController(
        ResourceRepository(root / "app-data"),
        tm_adapter=adapter,
    )
    controller.set_project(
        EditorProject(
            name="Confirmed project",
            segments=(
                EditorSegment(
                    id="segment-1",
                    source="Raw source",
                    target="Raw target",
                    speaker="Narrator",
                ),
                EditorSegment(
                    id="segment-2",
                    source="Already confirmed",
                    target="Done",
                    confirmed=True,
                ),
                EditorSegment(
                    id="segment-3",
                    source="Next untranslated",
                ),
            ),
        )
    )
    return controller, adapter


class EditorControllerTMConfirmTests(unittest.TestCase):
    def test_success_uses_adapter_report_then_confirms_and_navigates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.first"),
                _config(root, "canonical.second"),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            call_log: list[str] = []
            legacy = _RecordingLegacyBackend(
                "legacy.first",
                call_log=call_log,
                backing_path=configs[0].path,
            )
            canonical = _RecordingStore(
                "canonical.second",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            controller, adapter = _controller(
                root,
                configs,
                {
                    configs[0].path: _binding(legacy),
                    configs[1].path: _canonical_binding(canonical),
                },
            )

            original_append = EditorTMAdapter.append_confirmed

            def observed_append(
                instance: EditorTMAdapter,
                *,
                segment: EditorSegment,
                target: str,
                file_source: str,
            ) -> WriteReport:
                return original_append(
                    instance,
                    segment=segment,
                    target=target,
                    file_source=file_source,
                )

            with patch.object(
                EditorTMAdapter,
                "append_confirmed",
                autospec=True,
                side_effect=observed_append,
            ) as append_confirmed:
                result = controller.confirm_current()

            append_confirmed.assert_called_once_with(
                adapter,
                segment=EditorSegment(
                    id="segment-1",
                    source="Raw source",
                    target="Raw target",
                    speaker="Narrator",
                ),
                target="Raw target",
                file_source="Confirmed project",
            )
            self.assertEqual(call_log, ["legacy.first", "canonical.second"])
            self.assertEqual(
                result.write_report.written_resource_ids,
                ("legacy.first", "canonical.second"),
            )
            self.assertEqual(
                tuple(item.resource_id for item in result.write_report.outcomes),
                ("legacy.first", "canonical.second"),
            )
            self.assertTrue(result.project.segments[0].confirmed)
            self.assertEqual(result.current_index, 2)
            self.assertEqual(controller.current_index, 2)
            self.assertTrue(controller.dirty)

    def test_partial_failure_keeps_project_and_position_but_reports_all_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.failed"),
                _config(root, "canonical.written"),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            call_log: list[str] = []
            legacy = _RecordingLegacyBackend(
                "legacy.failed",
                call_log=call_log,
                backing_path=configs[0].path,
                error=OSError("/secret/private/path"),
            )
            canonical = _RecordingStore(
                "canonical.written",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            controller, _adapter_instance = _controller(
                root,
                configs,
                {
                    configs[0].path: _binding(legacy),
                    configs[1].path: _canonical_binding(canonical),
                },
            )
            project_before = controller.project
            index_before = controller.current_index
            dirty_before = controller.dirty

            result = controller.confirm_current()

            self.assertIs(result.project, project_before)
            self.assertEqual(controller.project, project_before)
            self.assertEqual(controller.current_index, index_before)
            self.assertEqual(controller.dirty, dirty_before)
            self.assertEqual(call_log, ["legacy.failed", "canonical.written"])
            self.assertEqual(
                result.write_report.written_resource_ids,
                ("canonical.written",),
            )
            self.assertEqual(
                result.write_report.errors,
                ("TM.WRITE.LEGACY_APPEND_FAILED",),
            )
            self.assertFalse(result.write_report.succeeded)
            self.assertNotIn("secret", repr(result.write_report).lower())
            self.assertNotIn(str(root), repr(result.write_report))

    def test_no_writable_tm_confirms_without_calls_or_byte_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configs = (
                _config(root, "legacy.no-update", update=False),
                _config(root, "canonical.no-update", update=False),
            )
            for config in configs:
                config.path.write_bytes(b"seed\n")
            before = {config.id: _digest(config.path) for config in configs}
            call_log: list[str] = []
            legacy = _RecordingLegacyBackend(
                "legacy.no-update",
                call_log=call_log,
                backing_path=configs[0].path,
            )
            canonical = _RecordingStore(
                "canonical.no-update",
                call_log=call_log,
                backing_path=configs[1].path,
            )
            controller, _adapter_instance = _controller(
                root,
                configs,
                {
                    configs[0].path: _binding(legacy),
                    configs[1].path: _canonical_binding(canonical),
                },
            )

            result = controller.confirm_current()

            self.assertEqual(result.write_report, WriteReport())
            self.assertTrue(result.project.segments[0].confirmed)
            self.assertEqual(result.current_index, 2)
            self.assertEqual(call_log, [])
            self.assertEqual(
                {config.id: _digest(config.path) for config in configs},
                before,
            )

    def test_unstructured_adapter_report_and_programmer_error_never_confirm(
        self,
    ) -> None:
        for response in (
            WriteReport(written_resource_ids=("forged",)),
            TypeError("programmer append invariant"),
        ):
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                configs = (_config(root, "legacy.write"),)
                configs[0].path.write_bytes(b"seed\n")
                backend = _RecordingLegacyBackend(
                    "legacy.write",
                    call_log=[],
                    backing_path=configs[0].path,
                )
                controller, adapter = _controller(
                    root,
                    configs,
                    {configs[0].path: _binding(backend)},
                )
                project_before = controller.project
                if isinstance(response, BaseException):
                    replacement = patch.object(
                        EditorTMAdapter,
                        "append_confirmed",
                        autospec=True,
                        side_effect=response,
                    )
                    expected = TypeError
                else:
                    replacement = patch.object(
                        EditorTMAdapter,
                        "append_confirmed",
                        autospec=True,
                        return_value=response,
                    )
                    expected = ValueError

                with replacement, self.assertRaises(expected):
                    controller.confirm_current()

                self.assertIs(controller.project, project_before)
                self.assertFalse(controller.project.segments[0].confirmed)
                self.assertEqual(controller.current_index, 0)
                self.assertFalse(controller.dirty)


if __name__ == "__main__":
    unittest.main()
