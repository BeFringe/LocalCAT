"""Task 5.1 current TM query epoch and issued-membership tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost, CapabilityHostComposition, compose_capability_host
import editor_controller as controller_module
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ImportRequest,
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

    def test_raw_speaker_change_advances_epoch_and_requeries_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = self._controller(
                Path(temporary)
            )
            _ = controller.tm_suggestion_report()
            epoch = controller.query_epoch

            object.__setattr__(controller.current_segment, "speaker", "Narrator")

            self.assertEqual(controller.query_epoch, epoch + 1)
            refreshed = controller.issued_tm_suggestions
            self.assertEqual(len(refreshed), 1)
            self.assertEqual(
                refreshed[0].query_identity.query_epoch,
                epoch + 1,
            )

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

    def test_persisted_update_refresh_failure_blocks_old_query_apply_and_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, repository = self._controller(
                Path(temporary)
            )
            termbase = controller.create_resource(
                "Unrelated terms",
                ResourceKind.TERMBASE,
            )
            issued = controller.tm_suggestion_report().suggestions[0]
            controller.update_target("Draft target")
            epoch = controller.query_epoch
            tm = next(
                item
                for item in repository.list_resources()
                if item.kind is ResourceKind.TRANSLATION_MEMORY
            )
            before_tm = tm.path.read_bytes()

            with patch.object(
                controller,
                "_load_glossary_engine",
                side_effect=ValueError("/secret/unrelated-termbase"),
            ):
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "TM.RUNTIME.REFRESH_FAILED",
                ) as update_failure:
                    controller.update_resource(replace(tm, update=False))
            self.assertNotIn("secret", str(update_failure.exception))

            self.assertFalse(repository.get(tm.id).update)
            self.assertEqual(controller.query_epoch, epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions, ())
            for operation in (
                controller.tm_suggestion_report,
                lambda: controller.apply_tm_suggestion(issued),
                controller.confirm_current,
            ):
                with self.subTest(operation=operation), self.assertRaisesRegex(
                    EditorControllerError,
                    "TM.RUNTIME.REFRESH_FAILED",
                ):
                    operation()
            self.assertEqual(tm.path.read_bytes(), before_tm)

            controller.reload_resources()

            self.assertEqual(
                controller.issued_tm_suggestions[0].target,
                "你好。",
            )
            result = controller.confirm_current()
            self.assertTrue(result.write_report.succeeded)
            self.assertEqual(result.write_report.written_resource_ids, ())
            self.assertEqual(tm.path.read_bytes(), before_tm)
            self.assertTrue(termbase.path.exists())

    def test_delete_refresh_failure_cannot_recreate_unregistered_tm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, repository = self._controller(
                Path(temporary)
            )
            _ = controller.create_resource(
                "Unrelated terms",
                ResourceKind.TERMBASE,
            )
            tm = next(
                item
                for item in repository.list_resources()
                if item.kind is ResourceKind.TRANSLATION_MEMORY
            )
            controller.update_target("Draft target")

            with patch.object(
                controller,
                "_load_glossary_engine",
                side_effect=ValueError("/secret/unrelated-termbase"),
            ):
                deleted = controller.delete_resource(tm.id)

            self.assertEqual(deleted.id, tm.id)
            self.assertFalse(tm.path.exists())
            self.assertNotIn(
                tm.id,
                tuple(item.id for item in repository.list_resources()),
            )
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.RUNTIME.REFRESH_FAILED",
            ):
                controller.confirm_current()
            self.assertFalse(tm.path.exists())

            controller.reload_resources()

            self.assertEqual(controller.issued_tm_suggestions, ())
            self.assertFalse(tm.path.exists())

    def test_create_refresh_failure_latches_persisted_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, repository = self._controller(
                Path(temporary)
            )
            _ = controller.tm_suggestion_report()

            with patch.object(
                EditorTMAdapter,
                "_refresh_runtime",
                autospec=True,
                side_effect=ValueError("/secret/runtime-refresh"),
            ):
                with self.assertRaises(EditorControllerError):
                    controller.create_resource(
                        "Persisted TM",
                        ResourceKind.TRANSLATION_MEMORY,
                    )

            self.assertGreaterEqual(len(repository.list_resources()), 2)
            self.assertEqual(controller.issued_tm_suggestions, ())
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.RUNTIME.REFRESH_FAILED",
            ):
                controller.tm_suggestion_report()

            controller.reload_resources()
            self.assertTrue(controller.issued_tm_suggestions)

    def test_term_import_does_not_refresh_or_latch_unchanged_tm_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = self._controller(root)
            imported_resource = controller.create_resource(
                "Imported terms",
                ResourceKind.TERMBASE,
            )
            before = controller.tm_suggestion_report()
            source = root / "import.csv"
            source.write_text(
                "Source,Target\nHello,你好\n",
                encoding="utf-8-sig",
            )

            with patch.object(
                EditorTMAdapter,
                "_refresh_runtime",
                autospec=True,
                side_effect=ValueError("/secret/runtime-refresh"),
            ) as refresh:
                report = controller.import_resource(
                    ImportRequest(
                        resource_id=imported_resource.id,
                        input_path=source.resolve(),
                    )
                )

            after = controller.tm_suggestion_report()

        self.assertEqual(report.imported, 1)
        self.assertEqual(report.errors, ())
        refresh.assert_not_called()
        self.assertEqual(after.suggestions, before.suggestions)

    def test_refresh_programmer_error_propagates_but_latch_stays_body_free(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, repository = self._controller(
                Path(temporary)
            )
            tm = repository.list_resources()[0]
            _ = controller.tm_suggestion_report()

            with patch.object(
                EditorTMAdapter,
                "_refresh_runtime",
                autospec=True,
                side_effect=AssertionError("/secret/programmer-body"),
            ):
                with self.assertRaisesRegex(
                    AssertionError,
                    "programmer-body",
                ):
                    controller.update_resource(replace(tm, update=False))

            self.assertFalse(repository.get(tm.id).update)
            self.assertEqual(controller.issued_tm_suggestions, ())
            with self.assertRaisesRegex(
                EditorControllerError,
                "TM.RUNTIME.REFRESH_FAILED",
            ) as raised:
                controller.tm_suggestion_report()
            self.assertNotIn("secret", str(raised.exception))

    def test_query_cannot_cross_persisted_update_and_failed_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, repository = self._controller(
                Path(temporary)
            )
            _ = controller.create_resource(
                "Unrelated terms",
                ResourceKind.TERMBASE,
            )
            tm = next(
                item
                for item in repository.list_resources()
                if item.kind is ResourceKind.TRANSLATION_MEMORY
            )
            refresh_entered = Event()
            refresh_release = Event()
            query_done = Event()
            update_errors: list[BaseException] = []
            query_errors: list[BaseException] = []
            query_reports: list[object] = []

            def fail_glossary_refresh(_path: Path):
                refresh_entered.set()
                if not refresh_release.wait(5.0):
                    raise AssertionError("test refresh release timed out")
                raise ValueError("forced glossary refresh failure")

            def update_resource() -> None:
                try:
                    controller.update_resource(replace(tm, update=False))
                except BaseException as error:
                    update_errors.append(error)

            def query_current() -> None:
                try:
                    query_reports.append(controller.tm_suggestion_report())
                except BaseException as error:
                    query_errors.append(error)
                finally:
                    query_done.set()

            with patch.object(
                controller,
                "_load_glossary_engine",
                side_effect=fail_glossary_refresh,
            ):
                update_thread = Thread(target=update_resource, daemon=True)
                update_thread.start()
                self.assertTrue(refresh_entered.wait(5.0))
                query_thread = Thread(target=query_current, daemon=True)
                query_thread.start()
                try:
                    self.assertFalse(query_done.wait(0.1))
                finally:
                    refresh_release.set()
                update_thread.join(5.0)
                query_thread.join(5.0)

            self.assertFalse(update_thread.is_alive())
            self.assertFalse(query_thread.is_alive())
            self.assertEqual(len(update_errors), 1)
            self.assertIs(type(update_errors[0]), EditorControllerError)
            self.assertEqual(query_reports, [])
            self.assertEqual(len(query_errors), 1)
            self.assertEqual(
                str(query_errors[0]),
                "TM.RUNTIME.REFRESH_FAILED",
            )
            self.assertFalse(repository.get(tm.id).update)

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

    def test_observer_auto_requery_retries_inflight_runtime_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, adapter, runtime, repository = self._controller(
                Path(temporary)
            )
            first = controller.tm_suggestion_report()
            initial_epoch = first.query_identity.query_epoch
            _ = runtime.refresh(repository.list_resources())
            original = EditorTMAdapter._query_current_operation
            calls = 0

            def query_with_second_refresh(current, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                operation = original(current, **kwargs)
                if current is adapter and calls == 1:
                    _ = runtime.refresh(repository.list_resources())
                return operation

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                new=query_with_second_refresh,
            ):
                refreshed = controller.issued_tm_suggestions

            self.assertEqual(calls, 2)
            self.assertEqual(controller.query_epoch, initial_epoch + 2)
            self.assertEqual(len(refreshed), 1)
            self.assertEqual(
                refreshed[0].query_identity.query_epoch,
                initial_epoch + 2,
            )

    def test_gate_c_change_after_final_check_retries_before_report_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            composition = compose_capability_host(evaluated_at_utc=_EVALUATED_AT)
            controller, adapter, _runtime, _repository = self._controller(
                Path(temporary),
                composition=composition,
            )
            initial_epoch = controller.query_epoch
            gate_c = composition.retrieval_gate_c_validation_owner
            assert gate_c is not None
            original_clone = controller_module._clone_tm_suggestion_report
            original_query = EditorTMAdapter._query_current_operation
            clone_calls = 0
            query_calls = 0

            def clone_after_gate_c_change(report):  # type: ignore[no-untyped-def]
                nonlocal clone_calls
                clone_calls += 1
                cloned = original_clone(report)
                if clone_calls == 1:
                    _ = gate_c.validate_gate_c(
                        generated_at_utc=datetime(
                            2030,
                            1,
                            1,
                            10,
                            tzinfo=timezone.utc,
                        ),
                        valid_until_utc=datetime(
                            2030,
                            1,
                            2,
                            10,
                            tzinfo=timezone.utc,
                        ),
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                return cloned

            def counted_query(current, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal query_calls
                if current is adapter:
                    query_calls += 1
                return original_query(current, **kwargs)

            with (
                patch.object(
                    controller_module,
                    "_clone_tm_suggestion_report",
                    new=clone_after_gate_c_change,
                ),
                patch.object(
                    EditorTMAdapter,
                    "_query_current_operation",
                    new=counted_query,
                ),
            ):
                report = controller.tm_suggestion_report()

            self.assertEqual(clone_calls, 2)
            self.assertEqual(query_calls, 2)
            self.assertEqual(
                report.query_identity.query_epoch,
                initial_epoch + 1,
            )
            self.assertEqual(controller.query_epoch, initial_epoch + 1)
            self.assertEqual(controller.issued_tm_suggestions, report.suggestions)

    def test_capability_and_threshold_generation_changes_auto_requery(self) -> None:
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

            query_calls = 0
            original_query = EditorTMAdapter._query_current_operation

            def counted_query(current, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal query_calls
                query_calls += 1
                return original_query(current, **kwargs)

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                new=counted_query,
            ):
                automatically_refreshed = controller.issued_tm_suggestions

            self.assertEqual(controller.query_epoch, first_epoch + 1)
            self.assertEqual(query_calls, 1)
            self.assertTrue(automatically_refreshed)
            self.assertEqual(
                automatically_refreshed[0].query_identity.query_epoch,
                first_epoch + 1,
            )
            self.assertEqual(
                tuple(
                    (item.resource_id, item.record_id, item.target)
                    for item in automatically_refreshed
                ),
                tuple(
                    (item.resource_id, item.record_id, item.target)
                    for item in first.suggestions
                ),
            )

            _ = controller.workspace_state.update_tm_preferences(
                TMPreferences(minimum_similarity=0.75)
            )
            threshold_suggestions = controller.issued_tm_suggestions
            self.assertEqual(controller.query_epoch, first_epoch + 2)
            self.assertEqual(
                threshold_suggestions[0].query_identity.query_epoch,
                first_epoch + 2,
            )


if __name__ == "__main__":
    unittest.main()
