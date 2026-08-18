"""Task 5.2 issued TM suggestion apply and zero-mutation tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

import editor_controller as controller_module
from capability_host import (
    CapabilityHost,
    CapabilityHostComposition,
    compose_capability_host,
)
from editor_contracts import (
    EditorProject,
    EditorSegment,
    LegacyExactTMSuggestion,
    ResourceKind,
    TMPreferences,
    TMResourceDisplayMode,
    TMSuggestion,
)
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import TMResourceResolver, TMRuntimeHost
from tm_contracts import TMMatchType
from tests.test_capability_host_gate_d import (
    _FakeGateDExecution,
    _composition as _gate_d_composition,
    _gate_c,
    _gate_d_owner,
)
from tests.test_editor_tm_adapter_canonical import _activate


_EVALUATED_AT = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)


def _legacy_fixture(
    root: Path,
    *,
    capability_host: CapabilityHost | None = None,
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
            capability_host
            if capability_host is not None
            else CapabilityHost(evaluated_at_utc=_EVALUATED_AT)
        ),
    )
    controller = EditorController(repository, tm_adapter=adapter)
    controller.set_project(
        EditorProject(
            name="Apply",
            segments=(
                EditorSegment(
                    id="segment-1",
                    source="Hello.",
                    target="旧译文",
                    confirmed=True,
                ),
                EditorSegment(
                    id="segment-2",
                    source="Other.",
                    target="第二段",
                    confirmed=True,
                ),
            ),
        )
    )
    return controller, adapter, runtime, repository


def _canonical_controller(
    test_case: unittest.TestCase,
    root: Path,
) -> tuple[
    EditorController,
    TMRuntimeHost,
    CapabilityHostComposition,
]:
    source = _activate(
        root,
        resource_id="local-tm",
        rows=(
            '{"source":"aabba","target":"context","speaker":"Narrator"}',
            '{"source":"aabba","target":"exact","speaker":"Other"}',
            '{"source":"bbaab","target":"boundary"}',
            '{"source":"AABBA","target":"one-hundred"}',
        ),
    )
    repository = ResourceRepository(
        root / "app-data",
        default_tm_path=source,
    )
    runtime = TMRuntimeHost(
        resolver=TMResourceResolver(),
        configs=repository.list_resources(),
    )
    execution = _FakeGateDExecution()
    composition = _gate_d_composition(test_case, execution)
    _ = _gate_c(composition)
    gate_d = _gate_d_owner(composition)
    _ = gate_d.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
    status = gate_d.wait(timeout=10.0)
    test_case.assertEqual(status.state.value, "SUCCEEDED")
    controller = EditorController(
        repository,
        tm_adapter=EditorTMAdapter(
            runtime_host=runtime,
            capability_host=composition.host,
        ),
    )
    controller.set_project(
        EditorProject(
            name="Canonical apply",
            segments=(
                EditorSegment(
                    id="segment-canonical",
                    source="aabba",
                    target="旧 canonical 译文",
                    speaker="Narrator",
                    confirmed=True,
                ),
            ),
        )
    )
    return controller, runtime, composition


def _regular_file_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        snapshot[str(path.relative_to(root))] = (
            len(data),
            hashlib.sha256(data).hexdigest(),
        )
    return snapshot


def _editor_state(controller: EditorController) -> tuple[object, int, bool]:
    return controller.project, controller.current_index, controller.dirty


class EditorControllerTMSuggestionApplyTests(unittest.TestCase):
    def test_current_issued_exact_applies_only_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = _legacy_fixture(root)
            report = controller.tm_suggestion_report()
            suggestion = report.suggestions[0]
            current_index = controller.current_index
            file_snapshot = _regular_file_snapshot(root)

            project = controller.apply_tm_suggestion(suggestion)

            self.assertEqual(project.segments[0].target, "你好。")
            self.assertFalse(project.segments[0].confirmed)
            self.assertTrue(controller.dirty)
            self.assertEqual(controller.current_index, current_index)
            self.assertEqual(_regular_file_snapshot(root), file_snapshot)

    def test_value_equal_defensive_clone_is_accepted_as_issued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _adapter, _runtime, _repository = _legacy_fixture(
                Path(temporary)
            )
            suggestion = controller.tm_suggestion_report().suggestions[0]
            clone = replace(
                suggestion,
                provenance=replace(suggestion.provenance),
                query_identity=replace(suggestion.query_identity),
            )

            self.assertIsNot(clone, suggestion)
            project = controller.apply_tm_suggestion(clone)

            self.assertEqual(project.segments[0].target, suggestion.target)
            self.assertFalse(project.segments[0].confirmed)

    def test_authentic_exact_context_and_fuzzy_require_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _runtime, _composition = _canonical_controller(
                self,
                root,
            )
            before_query_target = controller.current_segment.target
            report = controller.tm_suggestion_report()
            by_type = {item.match_type: item for item in report.suggestions}
            self.assertEqual(controller.current_segment.target, before_query_target)
            self.assertTrue(
                {
                    TMMatchType.EXACT,
                    TMMatchType.CONTEXT,
                    TMMatchType.FUZZY,
                }.issubset(by_type)
            )
            file_snapshot = _regular_file_snapshot(root)
            current_index = controller.current_index

            for match_type in (
                TMMatchType.EXACT,
                TMMatchType.CONTEXT,
                TMMatchType.FUZZY,
            ):
                suggestion = by_type[match_type]
                project = controller.apply_tm_suggestion(suggestion)
                self.assertEqual(project.segments[0].target, suggestion.target)
                self.assertFalse(project.segments[0].confirmed)
                self.assertTrue(controller.dirty)
                self.assertEqual(controller.current_index, current_index)
                self.assertEqual(_regular_file_snapshot(root), file_snapshot)

    def test_legal_field_substitutions_are_rejected_with_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _runtime, _composition = _canonical_controller(
                self,
                root,
            )
            report = controller.tm_suggestion_report()
            exact = next(
                item for item in report.suggestions
                if item.match_type is TMMatchType.EXACT
            )
            fuzzy = next(
                item for item in report.suggestions
                if item.match_type is TMMatchType.FUZZY
            )
            candidates = {
                "resource": replace(exact, resource_id="tm.other"),
                "record": replace(exact, record_id="canonical:999999"),
                "target": replace(exact, target="替换后的目标"),
                "type": replace(exact, match_type=TMMatchType.CONTEXT),
                "score": replace(fuzzy, final_similarity=0.95),
                "matched_source": replace(
                    fuzzy,
                    matched_source="different source",
                ),
                "query_source": replace(
                    exact,
                    query_source="Changed source",
                    matched_source="Changed source",
                ),
                "provenance_name": replace(
                    exact,
                    provenance=replace(
                        exact.provenance,
                        resource_name="Other TM",
                    ),
                ),
                "provenance_mode": replace(
                    exact,
                    provenance=replace(
                        exact.provenance,
                        resource_mode=TMResourceDisplayMode.SOURCE_DIVERGED,
                    ),
                ),
                "epoch": replace(
                    exact,
                    query_identity=replace(
                        exact.query_identity,
                        query_epoch=exact.query_identity.query_epoch + 1,
                    ),
                ),
            }
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            for label, candidate in candidates.items():
                with self.subTest(label=label):
                    candidate.__post_init__()
                    with self.assertRaises(EditorControllerError):
                        controller.apply_tm_suggestion(candidate)
                    self.assertEqual(_editor_state(controller), initial_editor)
                    self.assertEqual(_regular_file_snapshot(root), initial_files)

    def test_object_level_tamper_does_not_change_private_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = _legacy_fixture(root)
            suggestion = controller.tm_suggestion_report().suggestions[0]
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)
            object.__setattr__(suggestion, "target", "tampered target")

            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(suggestion)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)

    def test_project_segment_source_runtime_and_threshold_stale_are_rejected(self) -> None:
        triggers = ("project", "segment", "source", "runtime", "threshold")
        for trigger in triggers:
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                controller, _adapter, runtime, repository = _legacy_fixture(root)
                suggestion = controller.tm_suggestion_report().suggestions[0]
                if trigger == "project":
                    controller.set_project(
                        EditorProject(
                            name="New session",
                            segments=(EditorSegment(id="new", source="Hello."),),
                        )
                    )
                elif trigger == "segment":
                    controller.move(1)
                elif trigger == "source":
                    object.__setattr__(
                        controller.current_segment,
                        "source",
                        "Changed source.",
                    )
                elif trigger == "runtime":
                    runtime.refresh(repository.list_resources())
                else:
                    _ = controller.workspace_state.update_tm_preferences(
                        TMPreferences(minimum_similarity=0.75)
                    )
                initial_editor = _editor_state(controller)
                initial_files = _regular_file_snapshot(root)

                with self.assertRaises(EditorControllerError):
                    controller.apply_tm_suggestion(suggestion)

                self.assertEqual(_editor_state(controller), initial_editor)
                self.assertEqual(_regular_file_snapshot(root), initial_files)
                refreshed = controller.issued_tm_suggestions
                self.assertNotIn(suggestion, refreshed)
                self.assertTrue(
                    all(
                        item.query_identity.query_epoch
                        > suggestion.query_identity.query_epoch
                        for item in refreshed
                    )
                )

    def test_capability_generation_stale_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            composition = compose_capability_host(evaluated_at_utc=_EVALUATED_AT)
            controller, _adapter, _runtime, _repository = _legacy_fixture(
                root,
                capability_host=composition.host,
            )
            suggestion = controller.tm_suggestion_report().suggestions[0]
            gate_c = composition.retrieval_gate_c_validation_owner
            assert gate_c is not None
            _ = gate_c.validate_gate_c(
                generated_at_utc=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                valid_until_utc=datetime(2030, 1, 2, 10, tzinfo=timezone.utc),
                evaluated_at_utc=_EVALUATED_AT,
            )
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(suggestion)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)
            refreshed = controller.issued_tm_suggestions
            self.assertEqual(len(refreshed), 1)
            self.assertGreater(
                refreshed[0].query_identity.query_epoch,
                suggestion.query_identity.query_epoch,
            )

    def test_programmer_assertion_is_not_laundered_or_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = _legacy_fixture(root)
            suggestion = controller.tm_suggestion_report().suggestions[0]
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            with patch.object(
                controller_module,
                "_clone_tm_suggestion",
                side_effect=AssertionError("programmer invariant"),
            ):
                with self.assertRaisesRegex(AssertionError, "programmer invariant"):
                    controller.apply_tm_suggestion(suggestion)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)

    def test_programmer_type_error_is_not_laundered_or_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = _legacy_fixture(root)
            suggestion = controller.tm_suggestion_report().suggestions[0]
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            with patch.object(
                controller_module,
                "_clone_tm_suggestion",
                side_effect=TypeError("programmer type invariant"),
            ):
                with self.assertRaisesRegex(TypeError, "programmer type invariant"):
                    controller.apply_tm_suggestion(suggestion)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)

    def test_commit_value_error_is_not_laundered_or_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, _repository = _legacy_fixture(root)
            suggestion = controller.tm_suggestion_report().suggestions[0]
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            with patch.object(
                controller,
                "update_target",
                side_effect=ValueError("programmer value invariant"),
            ):
                with self.assertRaisesRegex(ValueError, "programmer value invariant"):
                    controller.apply_tm_suggestion(suggestion)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)

    def test_apply_linearizes_before_runtime_or_capability_refresh(self) -> None:
        for trigger in ("runtime", "capability"):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                composition = compose_capability_host(
                    evaluated_at_utc=_EVALUATED_AT
                )
                controller, _adapter, runtime, repository = _legacy_fixture(
                    root,
                    capability_host=composition.host,
                )
                suggestion = controller.tm_suggestion_report().suggestions[0]
                apply_entered = Event()
                allow_apply = Event()
                refresh_finished = Event()
                thread_errors: list[BaseException] = []
                original_update_target = controller.update_target

                def paused_update_target(target: str) -> EditorProject:
                    apply_entered.set()
                    if not allow_apply.wait(5.0):
                        raise AssertionError("apply release was not signalled")
                    return original_update_target(target)

                def apply_worker() -> None:
                    try:
                        controller.apply_tm_suggestion(suggestion)
                    except BaseException as error:
                        thread_errors.append(error)

                def refresh_worker() -> None:
                    try:
                        if trigger == "runtime":
                            runtime.refresh(repository.list_resources())
                        else:
                            gate_c = (
                                composition.retrieval_gate_c_validation_owner
                            )
                            assert gate_c is not None
                            _ = gate_c.validate_gate_c(
                                generated_at_utc=datetime(
                                    2030, 1, 1, 10, tzinfo=timezone.utc
                                ),
                                valid_until_utc=datetime(
                                    2030, 1, 2, 10, tzinfo=timezone.utc
                                ),
                                evaluated_at_utc=_EVALUATED_AT,
                            )
                    except BaseException as error:
                        thread_errors.append(error)
                    finally:
                        refresh_finished.set()

                with patch.object(
                    controller,
                    "update_target",
                    side_effect=paused_update_target,
                ):
                    apply_thread = Thread(target=apply_worker)
                    refresh_thread = Thread(target=refresh_worker)
                    apply_thread.start()
                    try:
                        self.assertTrue(apply_entered.wait(5.0))
                        refresh_thread.start()
                        self.assertFalse(refresh_finished.wait(0.1))
                    finally:
                        allow_apply.set()
                        apply_thread.join(5.0)
                        if refresh_thread.ident is not None:
                            refresh_thread.join(5.0)

                self.assertFalse(apply_thread.is_alive())
                self.assertFalse(refresh_thread.is_alive())
                self.assertEqual(thread_errors, [])
                self.assertEqual(
                    controller.current_segment.target,
                    suggestion.target,
                )

    def test_unissued_legacy_bridge_cannot_bypass_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, _adapter, _runtime, repository = _legacy_fixture(root)
            resource = repository.list_resources()[0]
            forged = LegacyExactTMSuggestion(
                source="Hello.",
                target="伪造旧建议",
                resource_id=resource.id,
                resource_name=resource.name,
            )
            initial_editor = _editor_state(controller)
            initial_files = _regular_file_snapshot(root)

            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(forged)

            self.assertEqual(_editor_state(controller), initial_editor)
            self.assertEqual(_regular_file_snapshot(root), initial_files)


if __name__ == "__main__":
    unittest.main()
