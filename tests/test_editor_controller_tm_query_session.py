"""Task 5.1 current TM query epoch and issued-membership tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost, CapabilityHostComposition, compose_capability_host
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    TMPreferences,
    TMSuggestion,
)
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


class EditorControllerTMQuerySessionTests(unittest.TestCase):
    def _controller(
        self,
        root: Path,
        *,
        composition: CapabilityHostComposition | None = None,
    ) -> tuple[
        EditorController,
        EditorTMAdapter,
        TMRuntimeHost,
        ResourceRepository,
    ]:
        repository = ResourceRepository(root / "app-data")
        resource = repository.create_resource(
            "Primary TM",
            ResourceKind.TRANSLATION_MEMORY,
        )
        resource.path.write_text(
            json.dumps(
                {"source": "Hello.", "target": "你好。"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        runtime = TMRuntimeHost(
            resolver=TMResourceResolver(),
            configs=repository.list_resources(),
        )
        adapter = EditorTMAdapter(
            runtime_host=runtime,
            capability_host=(
                composition.host
                if composition is not None
                else CapabilityHost(evaluated_at_utc=_EVALUATED_AT)
            ),
        )
        controller = EditorController(repository, tm_adapter=adapter)
        controller.set_project(
            EditorProject(
                name="Session",
                segments=(
                    EditorSegment(id="segment-1", source="Hello."),
                    EditorSegment(id="segment-2", source="Other."),
                ),
            )
        )
        return controller, adapter, runtime, repository

    def test_controller_exposes_one_tm_query_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )

            report = controller.tm_suggestion_report()

            self.assertEqual(report.query_identity.project_session_id, controller.project_session_id)
            self.assertEqual(report.query_identity.query_epoch, controller.query_epoch)
            self.assertEqual(controller.issued_tm_suggestions, report.suggestions)
            self.assertEqual(len(report.suggestions), 1)
            self.assertTrue(
                all(type(suggestion) is TMSuggestion for suggestion in report.suggestions)
            )

    def test_same_state_requery_keeps_epoch_membership_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )
            first = controller.tm_suggestion_report()
            first_epoch = controller.query_epoch

            second = controller.tm_suggestion_report()

            self.assertEqual(controller.query_epoch, first_epoch)
            self.assertEqual(second.suggestions, first.suggestions)
            self.assertEqual(
                tuple(
                    (item.resource_id, item.record_id)
                    for item in second.suggestions
                ),
                tuple(
                    (item.resource_id, item.record_id)
                    for item in first.suggestions
                ),
            )
            self.assertEqual(controller.issued_tm_suggestions, second.suggestions)

    def test_report_and_returned_membership_cannot_mutate_private_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )
            report = controller.tm_suggestion_report()
            original_target = report.suggestions[0].target
            returned = controller.issued_tm_suggestions

            object.__setattr__(report.suggestions[0], "target", "report tamper")
            object.__setattr__(returned[0], "target", "membership tamper")

            fresh = controller.issued_tm_suggestions
            self.assertEqual(fresh[0].target, original_target)
            self.assertIsNot(fresh[0], report.suggestions[0])
            self.assertIsNot(fresh[0], returned[0])

    def test_project_segment_and_source_changes_clear_membership_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )
            _ = controller.tm_suggestion_report()
            session = controller.project_session_id
            epoch = controller.query_epoch

            controller.move(1)
            self.assertEqual(controller.query_epoch, epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions, ())
            controller.go_to(1)
            self.assertEqual(controller.query_epoch, epoch + 1)

            current = controller.current_segment
            object.__setattr__(current, "source", "Other changed.")
            self.assertEqual(controller.query_epoch, epoch + 2)
            self.assertEqual(controller.issued_tm_suggestions, ())

            controller.set_project(
                EditorProject(
                    name="Replacement",
                    segments=(EditorSegment(id="new", source="Hello."),),
                )
            )
            self.assertNotEqual(controller.project_session_id, session)
            self.assertEqual(controller.query_epoch, epoch + 3)
            controller.close_project()
            self.assertEqual(controller.query_epoch, epoch + 4)

    def test_raw_speaker_change_advances_epoch_and_invalidates_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )
            _ = controller.tm_suggestion_report()
            epoch = controller.query_epoch

            object.__setattr__(controller.current_segment, "speaker", "Narrator")

            self.assertEqual(controller.query_epoch, epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions, ())

    def test_raw_speaker_change_invalidates_legacy_membership_without_adapter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = ResourceRepository(root / "app-data")
            resource = repository.create_resource(
                "Legacy TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            resource.path.write_text(
                json.dumps({"source": "Hello.", "target": "你好。"}) + "\n",
                encoding="utf-8",
            )
            controller = EditorController(repository)
            controller.set_project(
                EditorProject(
                    name="Legacy membership",
                    segments=(EditorSegment(id="segment-1", source="Hello."),),
                )
            )
            suggestion = controller.suggestions().tm_matches[0]

            object.__setattr__(
                controller.current_segment,
                "speaker",
                "Narrator",
            )

            with self.assertRaisesRegex(EditorControllerError, "stale"):
                controller.apply_tm_suggestion(suggestion)
            self.assertEqual(controller.current_segment.target, "")

    def test_controller_resource_update_refreshes_runtime_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, adapter, runtime, repository = self._controller(
                Path(temporary)
            )
            initial = controller.tm_suggestion_report()
            initial_epoch = initial.query_identity.query_epoch
            initial_generation = runtime.capture_operation_snapshot().generation
            resource = repository.list_resources()[0]

            query_calls = 0
            original_query = EditorTMAdapter._query_current_operation

            def counted_query(current, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal query_calls
                if current is adapter:
                    query_calls += 1
                return original_query(current, **kwargs)

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                new=counted_query,
            ):
                updated = controller.update_resource(
                    replace(resource, lookup=False, update=False)
                )

            self.assertFalse(updated.lookup)
            self.assertFalse(updated.update)
            refreshed_runtime = runtime.capture_operation_snapshot()
            self.assertEqual(
                refreshed_runtime.generation,
                initial_generation + 1,
            )
            self.assertFalse(refreshed_runtime.legacy_ports[0].lookup)
            self.assertFalse(refreshed_runtime.legacy_ports[0].update)
            self.assertEqual(controller.query_epoch, initial_epoch + 1)
            self.assertEqual(query_calls, 1)
            self.assertEqual(controller.issued_tm_suggestions, ())
            self.assertEqual(controller.tm_suggestion_report().suggestions, ())

    def test_runtime_refresh_invalidates_and_inflight_query_retries_new_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, adapter, runtime, repository = self._controller(
                Path(temporary)
            )
            initial_epoch = controller.query_epoch
            original = EditorTMAdapter._query_current_operation
            calls = 0

            def query_with_refresh(current, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                if current is not adapter:
                    return original(current, **kwargs)
                calls += 1
                operation = original(current, **kwargs)
                if calls == 1:
                    runtime.refresh(repository.list_resources())
                return operation

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                new=query_with_refresh,
            ):
                report = controller.tm_suggestion_report()

            self.assertEqual(calls, 2)
            self.assertEqual(controller.query_epoch, initial_epoch + 1)
            self.assertEqual(report.query_identity.query_epoch, initial_epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions, report.suggestions)

    def test_capability_and_threshold_generation_changes_clear_then_requery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            composition = compose_capability_host(evaluated_at_utc=_EVALUATED_AT)
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary),
                composition=composition,
            )
            first = controller.tm_suggestion_report()
            first_epoch = controller.query_epoch

            gate_c = composition.retrieval_gate_c_validation_owner
            assert gate_c is not None
            _ = gate_c.validate_gate_c(
                generated_at_utc=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                valid_until_utc=datetime(2030, 1, 2, 10, tzinfo=timezone.utc),
                evaluated_at_utc=_EVALUATED_AT,
            )

            self.assertEqual(controller.issued_tm_suggestions, ())
            self.assertEqual(controller.query_epoch, first_epoch + 1)
            refreshed = controller.tm_suggestion_report()
            self.assertEqual(
                refreshed.query_identity.query_epoch,
                first_epoch + 1,
            )
            self.assertEqual(
                tuple(
                    (item.resource_id, item.record_id, item.target)
                    for item in refreshed.suggestions
                ),
                tuple(
                    (item.resource_id, item.record_id, item.target)
                    for item in first.suggestions
                ),
            )

            _ = controller.workspace_state.update_tm_preferences(
                TMPreferences(minimum_similarity=0.75)
            )
            self.assertEqual(controller.issued_tm_suggestions, ())
            self.assertEqual(controller.query_epoch, first_epoch + 2)
            threshold_report = controller.tm_suggestion_report()
            self.assertEqual(
                threshold_report.query_identity.query_epoch,
                first_epoch + 2,
            )


if __name__ == "__main__":
    unittest.main()
