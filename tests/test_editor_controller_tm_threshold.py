"""Task 5.6 Controller-owned TM threshold update tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMPreferences,
    TMThresholdUpdateOutcome,
)
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from workspace_state import WorkspaceStateRepository


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class _FloatSubclass(float):
    pass


def _fixture(root: Path) -> tuple[EditorController, EditorTMAdapter]:
    repository = ResourceRepository(root / "app-data")
    resource = repository.create_resource(
        "Primary TM",
        ResourceKind.TRANSLATION_MEMORY,
    )
    resource.path.write_text(
        json.dumps({"source": "Hello", "target": "你好"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    adapter = EditorTMAdapter(
        runtime_host=TMRuntimeHost(
            resolver=TMResourceResolver(),
            configs=repository.list_resources(),
        ),
        capability_host=CapabilityHost(evaluated_at_utc=_EVALUATED_AT),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(
        EditorProject(
            name="Threshold",
            segments=(
                EditorSegment(id="segment-1", source="Hello"),
                EditorSegment(id="segment-2", source="Other"),
            ),
        )
    )
    return controller, adapter


class TMThresholdUpdateOutcomeTests(unittest.TestCase):
    def test_outcome_is_frozen_body_free_and_revalidates_nested_values(self) -> None:
        failed = TMThresholdUpdateOutcome(
            succeeded=False,
            preferences=TMPreferences(),
            safe_code="TM.THRESHOLD.INVALID",
        )
        self.assertFalse(failed.succeeded)
        self.assertEqual(failed.preferences.minimum_similarity, 0.60)
        with self.assertRaises(FrozenInstanceError):
            failed.succeeded = True  # pyright: ignore[reportAttributeAccessIssue]

        object.__setattr__(failed.preferences, "minimum_similarity", 0.50)
        with self.assertRaises(ValueError):
            failed.__post_init__()


class EditorControllerTMThresholdTests(unittest.TestCase):
    def test_boundaries_persist_advance_once_and_publish_fresh_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, adapter = _fixture(Path(temporary))
            initial = controller.tm_suggestion_report()
            initial_epoch = initial.query_identity.query_epoch
            observed_preferences: list[TMPreferences] = []
            original_query = EditorTMAdapter._query_current_operation

            def record_query_preferences(
                current: EditorTMAdapter,
                *,
                segment: EditorSegment,
                project_session_id: str,
                query_epoch: int,
                preferences: TMPreferences,
            ):
                observed_preferences.append(preferences)
                return original_query(
                    current,
                    segment=segment,
                    project_session_id=project_session_id,
                    query_epoch=query_epoch,
                    preferences=preferences,
                )

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                autospec=True,
                side_effect=record_query_preferences,
            ):
                upper = controller.update_tm_minimum_similarity(1.00)

            self.assertTrue(upper.succeeded)
            self.assertIsNone(upper.safe_code)
            self.assertEqual(upper.preferences, TMPreferences(1.00))
            self.assertEqual(observed_preferences, [TMPreferences(1.00)])
            self.assertEqual(
                controller.issued_tm_suggestions[0].query_identity.query_epoch,
                initial_epoch + 1,
            )
            self.assertEqual(controller.query_epoch, initial_epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions[0].target, "你好")
            self.assertEqual(controller.tm_preferences(), TMPreferences(1.00))
            self.assertEqual(
                WorkspaceStateRepository(
                    controller.repository.config_dir
                ).tm_preferences(),
                TMPreferences(1.00),
            )

            controller.set_project(
                EditorProject(
                    name="Next project",
                    segments=(EditorSegment(id="next", source="Hello"),),
                )
            )
            self.assertEqual(controller.tm_preferences(), TMPreferences(1.00))
            lower = controller.update_tm_minimum_similarity(0.60)
            self.assertTrue(lower.succeeded)
            self.assertEqual(lower.preferences, TMPreferences(0.60))

    def test_invalid_values_preserve_preference_epoch_and_current_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter = _fixture(Path(temporary))
            before_report = controller.tm_suggestion_report()
            before_suggestions = controller.issued_tm_suggestions
            before_epoch = controller.query_epoch

            invalid_values: tuple[object, ...] = (
                0.59,
                1.01,
                math.nan,
                math.inf,
                _FloatSubclass(0.75),
                1,
                True,
                object(),
            )
            for value in invalid_values:
                with self.subTest(value=value):
                    outcome = controller.update_tm_minimum_similarity(
                        value  # pyright: ignore[reportArgumentType]
                    )
                    self.assertFalse(outcome.succeeded)
                    self.assertEqual(outcome.safe_code, "TM.THRESHOLD.INVALID")
                    self.assertEqual(outcome.preferences, TMPreferences())
                    self.assertEqual(controller.query_epoch, before_epoch)
                    self.assertEqual(
                        controller.issued_tm_suggestions,
                        before_suggestions,
                    )

    def test_persistence_failure_preserves_value_bytes_epoch_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter = _fixture(Path(temporary))
            persisted = controller.update_tm_minimum_similarity(0.72)
            self.assertTrue(persisted.succeeded)
            before_report = controller.tm_suggestion_report()
            before_epoch = controller.query_epoch
            state_path = controller.workspace_state.state_path
            before_bytes = (
                state_path.read_bytes() if state_path.exists() else None
            )

            with patch(
                "workspace_state.os.replace",
                side_effect=OSError("/secret/disk/body"),
            ):
                outcome = controller.update_tm_minimum_similarity(0.88)

            self.assertFalse(outcome.succeeded)
            self.assertEqual(
                outcome.safe_code,
                "TM.THRESHOLD.PERSISTENCE_FAILED",
            )
            self.assertNotIn("secret", str(outcome))
            self.assertEqual(outcome.preferences, TMPreferences(0.72))
            self.assertEqual(controller.query_epoch, before_epoch)
            self.assertEqual(
                controller.issued_tm_suggestions,
                before_report.suggestions,
            )
            self.assertEqual(
                state_path.read_bytes() if state_path.exists() else None,
                before_bytes,
            )


if __name__ == "__main__":
    unittest.main()
