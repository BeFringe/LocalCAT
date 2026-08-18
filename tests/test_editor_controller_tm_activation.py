"""Task 5.4 TM activation preflight and operation lifecycle tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMActivationOperationView,
    TMActivationPreflightView,
)
from editor_controller import EditorController, EditorControllerError
from resource_repository import ResourceRepository
from tm_migration import MigrationPreflightError, TMMigrationService


def _tree_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


def _controller(
    root: Path,
) -> tuple[EditorController, ResourceRepository, str]:
    repository = ResourceRepository(root / "app-data")
    resource = repository.create_resource(
        "Legacy TM",
        ResourceKind.TRANSLATION_MEMORY,
    )
    resource.path.write_text(
        "\n".join(
            (
                json.dumps({"source": "Hello", "target": "你好"}),
                json.dumps({"source": "Hello", "target": "您好"}),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    controller = EditorController(repository)
    controller.set_project(
        EditorProject(
            name="Activation",
            segments=(
                EditorSegment(
                    id="segment-1",
                    source="Hello",
                    target="draft",
                ),
            ),
        )
    )
    return controller, repository, resource.id


class EditorControllerTMActivationTests(unittest.TestCase):
    def test_activation_views_are_frozen_closed_safe_contracts(self) -> None:
        with self.assertRaises(TypeError):
            TMActivationPreflightView(
                resource_id="tm.primary",
                resource_name="Primary",
                valid_count=True,  # type: ignore[arg-type]
                invalid_count=0,
                variant_count=0,
            )
        with self.assertRaises(ValueError):
            TMActivationOperationView(
                operation_id="0" * 32,
                resource_id="tm.primary",
                phase="ACTIVATING",
                completed=False,
                succeeded=True,
                safe_code=None,
                retryable=False,
            )
        with self.assertRaises(ValueError):
            TMActivationOperationView(
                operation_id="0" * 32,
                resource_id="tm.primary",
                phase="COMPLETED",
                completed=True,
                succeeded=False,
                safe_code="/secret/body",
                retryable=True,
            )

    def test_preflight_is_body_safe_read_only_and_cancellable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, repository, resource_id = _controller(root)
            before = _tree_snapshot(root)
            config_before = repository.list_resources()

            view = controller.prepare_tm_activation(resource_id)

            self.assertIs(type(view), TMActivationPreflightView)
            self.assertEqual(view.resource_id, resource_id)
            self.assertEqual(view.resource_name, "Legacy TM")
            self.assertEqual(view.valid_count, 2)
            self.assertEqual(view.invalid_count, 0)
            self.assertEqual(view.variant_count, 1)
            self.assertNotIn(str(root), repr(view))
            self.assertEqual(_tree_snapshot(root), before)
            self.assertEqual(repository.list_resources(), config_before)

            controller.cancel_tm_activation(view)

            self.assertEqual(_tree_snapshot(root), before)
            self.assertEqual(repository.list_resources(), config_before)
            with self.assertRaises(EditorControllerError):
                controller.activate_tm_resource(view)

    def test_stale_or_field_substituted_preflight_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, repository, resource_id = _controller(root)
            view = controller.prepare_tm_activation(resource_id)
            forged = replace(view, valid_count=view.valid_count + 1)

            with self.assertRaises(EditorControllerError):
                controller.activate_tm_resource(forged)

            resource = repository.get(resource_id)
            with resource.path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"source": "New", "target": "新"}) + "\n"
                )
            with self.assertRaises(EditorControllerError):
                controller.activate_tm_resource(view)
            self.assertIsNone(controller.tm_activation_operation())

    def test_real_activation_returns_safe_completed_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _repository, resource_id = _controller(root)
            preflight = controller.prepare_tm_activation(resource_id)

            started = controller.activate_tm_resource(preflight)
            completed = controller.wait_tm_activation(
                started.operation_id,
                timeout=20.0,
            )

            self.assertIs(type(started), TMActivationOperationView)
            self.assertEqual(started.phase, "ACTIVATING")
            self.assertFalse(started.completed)
            self.assertFalse(started.succeeded)
            self.assertEqual(completed.operation_id, started.operation_id)
            self.assertEqual(completed.phase, "COMPLETED")
            self.assertTrue(completed.completed)
            self.assertTrue(completed.succeeded)
            self.assertIsNone(completed.safe_code)
            self.assertNotIn(str(root), repr(completed))

    def test_running_operation_is_singleflight_and_does_not_block_editing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _repository, resource_id = _controller(root)
            preflight = controller.prepare_tm_activation(resource_id)
            entered = Event()
            release = Event()
            original_activate = TMMigrationService.activate_initial

            def blocked_activate(
                service: TMMigrationService,
                source: Path,
                exact_resource_id: str,
            ):  # type: ignore[no-untyped-def]
                entered.set()
                if not release.wait(10.0):
                    raise AssertionError("activation release was not signalled")
                return original_activate(service, source, exact_resource_id)

            with patch.object(
                TMMigrationService,
                "activate_initial",
                autospec=True,
                side_effect=blocked_activate,
            ):
                started = controller.activate_tm_resource(preflight)
                try:
                    self.assertTrue(entered.wait(5.0))
                    object.__setattr__(started, "phase", "COMPLETED")
                    private_running = controller.tm_activation_operation()
                    assert private_running is not None
                    self.assertEqual(private_running.phase, "ACTIVATING")
                    with self.assertRaises(EditorControllerError):
                        controller.activate_tm_resource(preflight)
                    with self.assertRaises(EditorControllerError):
                        controller.cancel_tm_activation(preflight)
                    controller.update_target("editing remains available")
                    self.assertEqual(
                        controller.current_segment.target,
                        "editing remains available",
                    )
                    running = controller.tm_activation_operation()
                    assert running is not None
                    self.assertEqual(running.phase, "ACTIVATING")
                finally:
                    release.set()

                completed = controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                )

            self.assertTrue(completed.succeeded)

    def test_safe_core_failure_and_programmer_error_have_closed_status(self) -> None:
        scenarios = (
            MigrationPreflightError("MIGRATION.SOURCE_CHANGED"),
            AssertionError("/secret/programmer/body"),
        )
        for error in scenarios:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                controller, _repository, resource_id = _controller(root)
                preflight = controller.prepare_tm_activation(resource_id)
                with patch.object(
                    TMMigrationService,
                    "activate_initial",
                    autospec=True,
                    side_effect=error,
                ):
                    started = controller.activate_tm_resource(preflight)
                    if type(error) is AssertionError:
                        with self.assertRaisesRegex(
                            AssertionError,
                            "/secret/programmer/body",
                        ):
                            controller.wait_tm_activation(
                                started.operation_id,
                                timeout=10.0,
                            )
                    else:
                        completed = controller.wait_tm_activation(
                            started.operation_id,
                            timeout=10.0,
                        )
                        self.assertEqual(
                            completed.safe_code,
                            "MIGRATION.SOURCE_CHANGED",
                        )

                status = controller.tm_activation_operation()
                assert status is not None
                self.assertTrue(status.completed)
                self.assertFalse(status.succeeded)
                self.assertNotIn(str(root), repr(status))
                self.assertNotIn("secret", repr(status).lower())


if __name__ == "__main__":
    unittest.main()
