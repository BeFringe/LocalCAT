"""Cluster E cumulative-review remediation journeys."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
import types
from typing import cast
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

import capability_host as capability_host_module
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    RetrievalDisplayState,
)
from editor_controller import EditorController, EditorControllerError
from editor_tm_adapter import EditorTMAdapter
import qt_editor
from qt_editor_window import QtEditorWindow
from qt_settings_dialog import QtSettingsDialog
from resource_repository import ResourceRepository
from tests.test_capability_host_gate_d import (
    _FakeGateDExecution,
    _composition as _gate_d_composition,
    _gate_c,
    _gate_d_owner,
)
from tests.test_editor_controller_tm_activation_completion import (
    _fixture as _activation_fixture,
)
from tests.test_editor_controller_tm_apply import (
    _EVALUATED_AT,
    _canonical_controller,
)
from tests.test_editor_tm_adapter_canonical import _activate
from tm_application_composition import TMResourceResolver, TMRuntimeHost


class ClusterERemediationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def test_real_bootstrap_composes_one_tm_runtime_and_keeps_refresh_live(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        class CapturingWindow(QWidget):
            def __init__(self, controller: object) -> None:
                super().__init__()
                captured["controller"] = controller
                self.pages = types.SimpleNamespace(
                    currentWidget=lambda: types.SimpleNamespace(
                        objectName=lambda: "editorPage"
                    )
                )
                self.segment_list = types.SimpleNamespace(count=lambda: 1)

        fake_window_module = types.ModuleType("qt_editor_window")
        setattr(fake_window_module, "QtEditorWindow", CapturingWindow)
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.dict(
                    sys.modules,
                    {"qt_editor_window": fake_window_module},
                ),
                patch.object(
                    capability_host_module,
                    "compose_capability_host",
                    wraps=capability_host_module.compose_capability_host,
                ) as compose,
                patch.object(
                    qt_editor,
                    "_start_capability_validation",
                    create=True,
                ) as start_validation,
            ):
                exit_code = qt_editor.main(
                    ["--sample", "--smoke-test", "--data-dir", temporary]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(compose.call_count, 1)
            self.assertEqual(start_validation.call_count, 1)
            controller = cast(EditorController, captured["controller"])
            self.assertTrue(controller.tm_suggestion_reports_enabled)
            report = controller.tm_suggestion_report()
            report.__post_init__()

            tm = next(
                resource
                for resource in controller.list_resources()
                if resource.kind is ResourceKind.TRANSLATION_MEMORY
            )
            updated = controller.update_resource(
                replace(tm, lookup=not tm.lookup)
            )
            self.assertIs(updated.lookup, not tm.lookup)
            self.assertIn(
                tm.id,
                {
                    status.resource_id
                    for status in controller.tm_resource_statuses()
                },
            )

    def test_gate_d_open_before_query_refreshes_one_same_generation_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _activate(
                root,
                resource_id="local-tm",
                rows=(
                    '{"source":"aabba","target":"exact"}',
                    '{"source":"bbaab","target":"fuzzy"}',
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
            composition = _gate_d_composition(self, execution)
            _ = _gate_c(composition)
            controller = EditorController(
                repository,
                tm_adapter=EditorTMAdapter(
                    runtime_host=runtime,
                    capability_host=composition.host,
                ),
            )
            controller.set_project(
                EditorProject(
                    name="Gate D projection",
                    segments=(EditorSegment(id="one", source="aabba"),),
                )
            )
            gate_d = _gate_d_owner(composition)
            _ = gate_d.start_gate_d(
                evaluated_at_utc=_EVALUATED_AT
            )
            self.assertEqual(gate_d.wait(timeout=10.0).state.value, "SUCCEEDED")

            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                autospec=True,
                side_effect=AssertionError("status APIs must not query"),
            ) as query:
                retrieval = controller.tm_retrieval_status()
                statuses = controller.tm_resource_statuses()
                self.assertEqual(controller._issued_tm_suggestions, ())

            self.assertEqual(query.call_count, 0)
            self.assertTrue(retrieval.fuzzy_available)
            self.assertEqual(len(statuses), 1)
            self.assertTrue(statuses[0].fuzzy_available)

    def test_gate_c_replacement_requeries_and_stales_old_fuzzy_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, composition = _canonical_controller(
                self,
                Path(temporary),
            )
            old_report = controller.tm_suggestion_report()
            old_fuzzy = next(
                suggestion
                for suggestion in old_report.suggestions
                if suggestion.match_type.value == "FUZZY"
            )

            _ = _gate_c(composition)
            with patch.object(
                EditorTMAdapter,
                "_query_current_operation",
                autospec=True,
                side_effect=AssertionError("status APIs must not query"),
            ) as query:
                statuses = controller.tm_resource_statuses()
                retrieval = controller.tm_retrieval_status()
                self.assertEqual(controller._issued_tm_suggestions, ())

            self.assertEqual(query.call_count, 0)
            self.assertFalse(retrieval.fuzzy_available)
            self.assertTrue(statuses)
            self.assertTrue(all(not status.fuzzy_available for status in statuses))
            with self.assertRaises(EditorControllerError):
                controller.apply_tm_suggestion(old_fuzzy)

    def test_status_flip_records_post_invalidation_baseline_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, composition = _canonical_controller(
                self,
                Path(temporary),
            )
            _ = controller.tm_suggestion_report()
            initial_epoch = controller._tm_query_epoch
            original_advance = controller._advance_tm_query_epoch
            changed_during_invalidation = False

            _ = _gate_c(composition)

            def advance_with_capability_flip() -> None:
                nonlocal changed_during_invalidation
                original_advance()
                if not changed_during_invalidation:
                    changed_during_invalidation = True
                    _ = _gate_c(composition)

            with (
                patch.object(
                    EditorTMAdapter,
                    "_query_current_operation",
                    autospec=True,
                    side_effect=AssertionError("status APIs must not query"),
                ) as query,
                patch.object(
                    controller,
                    "_advance_tm_query_epoch",
                    side_effect=advance_with_capability_flip,
                ) as advance,
            ):
                retrievals = (
                    controller.tm_retrieval_status(),
                    controller.tm_retrieval_status(),
                )
                resources = (
                    controller.tm_resource_statuses(),
                    controller.tm_resource_statuses(),
                )

            self.assertEqual(query.call_count, 0)
            self.assertEqual(advance.call_count, 1)
            self.assertEqual(controller._tm_query_epoch, initial_epoch + 1)
            self.assertEqual(controller._issued_tm_suggestions, ())
            self.assertEqual(retrievals[0], retrievals[1])
            self.assertEqual(resources[0], resources[1])

    def test_handoff_race_invalidates_once_without_epoch_churn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, composition = _canonical_controller(
                self,
                Path(temporary),
            )
            _ = controller.tm_suggestion_report()
            initial_epoch = controller._tm_query_epoch
            adapter = controller._tm_adapter
            self.assertIsNotNone(adapter)
            assert adapter is not None
            original_handoff = (
                adapter._inspect_retrieval_projection_for_controller
            )
            changed_before_handoff = False

            def handoff(
                _adapter: EditorTMAdapter,
            ) -> tuple[int, RetrievalDisplayState]:
                nonlocal changed_before_handoff
                if not changed_before_handoff:
                    changed_before_handoff = True
                    _ = _gate_c(composition)
                return original_handoff()

            with (
                patch.object(
                    EditorTMAdapter,
                    "_query_current_operation",
                    autospec=True,
                    side_effect=AssertionError("status APIs must not query"),
                ) as query,
                patch.object(
                    controller,
                    "_advance_tm_query_epoch",
                    wraps=controller._advance_tm_query_epoch,
                ) as advance,
                patch.object(
                    EditorTMAdapter,
                    "_inspect_retrieval_projection_for_controller",
                    autospec=True,
                    side_effect=handoff,
                ),
            ):
                first_retrieval = controller.tm_retrieval_status()
                first_resources = controller.tm_resource_statuses()
                second_retrieval = controller.tm_retrieval_status()
                second_resources = controller.tm_resource_statuses()

            self.assertEqual(query.call_count, 0)
            self.assertEqual(advance.call_count, 1)
            self.assertEqual(controller._tm_query_epoch, initial_epoch + 1)
            self.assertEqual(controller._issued_tm_suggestions, ())
            self.assertEqual(first_retrieval, second_retrieval)
            self.assertEqual(first_resources, second_resources)

    def test_capability_validation_programmer_error_reaches_thread_hook(
        self,
    ) -> None:
        for failing_stage, error in (
            ("matcher", TypeError("PRIVATE.MATCHER.PROOF")),
            ("gate_c", AssertionError("PRIVATE.GATE_C.PROOF")),
            ("gate_d", TypeError("PRIVATE.GATE_D.PROOF")),
        ):
            with self.subTest(stage=failing_stage):
                composition = capability_host_module.compose_capability_host(
                    evaluated_at_utc=datetime.now(timezone.utc)
                )
                entered = Event()
                release = Event()
                observed: list[object] = []

                gate_c = composition.retrieval_gate_c_validation_owner
                gate_d = composition.retrieval_gate_d_owner
                self.assertIsNotNone(gate_c)
                self.assertIsNotNone(gate_d)
                assert gate_c is not None
                assert gate_d is not None
                gate_d_owner = gate_d

                def result_or_fail(stage: str, result: object) -> object:
                    if stage == failing_stage:
                        entered.set()
                        self.assertTrue(release.wait(5.0))
                        raise error
                    return result

                def matcher(
                    _owner: object,
                    **_kwargs: object,
                ) -> object:
                    return result_or_fail(
                        "matcher",
                        composition.host.matcher_snapshot(),
                    )

                def retrieval(
                    _owner: object,
                    **_kwargs: object,
                ) -> object:
                    return result_or_fail(
                        "gate_c",
                        composition.host.retrieval_snapshot(),
                    )

                def benchmark(
                    _owner: object,
                    **_kwargs: object,
                ) -> object:
                    return result_or_fail("gate_d", gate_d_owner.status())

                def capture_hook(args: object) -> None:
                    observed.append(args)

                owner_type = type(composition.matcher_validation_owner)
                with (
                    patch.object(
                        owner_type,
                        "validate_text_v1",
                        autospec=True,
                        side_effect=matcher,
                    ),
                    patch.object(
                        type(gate_c),
                        "validate_gate_c",
                        autospec=True,
                        side_effect=retrieval,
                    ),
                    patch.object(
                        type(gate_d),
                        "start_gate_d",
                        autospec=True,
                        side_effect=benchmark,
                    ),
                    patch("threading.excepthook", side_effect=capture_hook),
                ):
                    worker = cast(
                        Thread,
                        qt_editor._start_capability_validation(composition),
                    )
                    self.assertTrue(entered.wait(5.0))
                    self.assertTrue(worker.daemon)
                    self.assertTrue(worker.is_alive())
                    self._events()
                    release.set()
                    worker.join(10.0)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(observed), 1)
                hook = observed[0]
                self.assertIs(getattr(hook, "exc_type", None), type(error))
                self.assertIn(
                    str(error),
                    str(getattr(hook, "exc_value", "")),
                )

    def test_capability_validation_preserves_owner_order(self) -> None:
        composition = capability_host_module.compose_capability_host(
            evaluated_at_utc=datetime.now(timezone.utc)
        )
        order: list[str] = []
        matcher_type = type(composition.matcher_validation_owner)
        gate_c = composition.retrieval_gate_c_validation_owner
        gate_d = composition.retrieval_gate_d_owner
        self.assertIsNotNone(gate_c)
        self.assertIsNotNone(gate_d)
        assert gate_c is not None
        assert gate_d is not None

        def matcher(_owner: object, **_kwargs: object) -> object:
            order.append("matcher")
            return composition.host.matcher_snapshot()

        def retrieval(_owner: object, **_kwargs: object) -> object:
            order.append("gate_c")
            return composition.host.retrieval_snapshot()

        def benchmark(_owner: object, **_kwargs: object) -> object:
            order.append("gate_d")
            return gate_d.status()

        with (
            patch.object(
                matcher_type,
                "validate_text_v1",
                autospec=True,
                side_effect=matcher,
            ),
            patch.object(
                type(gate_c),
                "validate_gate_c",
                autospec=True,
                side_effect=retrieval,
            ),
            patch.object(
                type(gate_d),
                "start_gate_d",
                autospec=True,
                side_effect=benchmark,
            ),
        ):
            worker = cast(
                Thread,
                qt_editor._start_capability_validation(composition),
            )
            worker.join(10.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(order, ["matcher", "gate_c", "gate_d"])

    def test_global_runtime_block_closes_both_tm_resources_not_last_operation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, repository, resource_a = _activation_fixture(
                Path(temporary)
            )
            preflight = controller.prepare_tm_activation(resource_a)
            started = controller.activate_tm_resource(preflight)
            self.assertTrue(
                controller.wait_tm_activation(
                    started.operation_id,
                    timeout=20.0,
                ).succeeded
            )
            resource_b = controller.create_resource(
                "Secondary TM",
                ResourceKind.TRANSLATION_MEMORY,
            )
            operation = controller.tm_activation_operation()
            self.assertIsNotNone(operation)
            assert operation is not None
            self.assertEqual(operation.resource_id, resource_a)

            with patch.object(
                EditorTMAdapter,
                "_refresh_runtime",
                autospec=True,
                side_effect=ValueError("/private/runtime/proof"),
            ):
                with self.assertRaisesRegex(
                    EditorControllerError,
                    "TM.RUNTIME.REFRESH_FAILED",
                ):
                    controller.update_resource(
                        replace(resource_b, update=False)
                    )

            statuses = controller.tm_resource_statuses()
            self.assertEqual(
                {status.resource_id for status in statuses},
                {resource_a, resource_b.id},
            )
            self.assertTrue(
                all(
                    status.mode.value == "UNAVAILABLE"
                    and not status.exact_available
                    and status.safe_codes == ("TM.RUNTIME.REFRESH_FAILED",)
                    for status in statuses
                )
            )
            retrieval = controller.tm_retrieval_status()
            self.assertFalse(retrieval.context_available)
            self.assertFalse(retrieval.fuzzy_available)
            self.assertEqual(
                retrieval.safe_codes,
                ("TM.RUNTIME.REFRESH_FAILED",),
            )
            self.assertFalse(repository.get(resource_b.id).update)

    def test_persisted_threshold_refresh_failure_is_typed_and_qt_truthful(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            window.show()
            self._events()
            chip = window.findChild(QPushButton, "tmThresholdChip")
            self.assertIsNotNone(chip)
            assert chip is not None
            resource = next(
                item
                for item in controller.list_resources()
                if item.kind is ResourceKind.TRANSLATION_MEMORY
            )
            update_threshold = controller.update_tm_minimum_similarity

            def block_then_update(value: float):
                with patch.object(
                    EditorTMAdapter,
                    "_refresh_runtime",
                    autospec=True,
                    side_effect=ValueError("/private/runtime/proof"),
                ):
                    with self.assertRaises(EditorControllerError):
                        controller.update_resource(
                            replace(resource, update=not resource.update)
                        )
                return update_threshold(value)

            with (
                patch(
                    "qt_tm_threshold.QInputDialog.getDouble",
                    return_value=(80, True),
                ),
                patch.object(
                    controller,
                    "update_tm_minimum_similarity",
                    side_effect=block_then_update,
                ),
            ):
                chip.click()
            self._events()

            self.assertEqual(
                controller.tm_preferences().minimum_similarity,
                0.80,
            )
            self.assertIn("80%", chip.text())
            self.assertFalse(bool(chip.property("fuzzyAvailable")))
            feedback = window.statusBar().currentMessage()
            self.assertIn("已保存", feedback)
            self.assertIn("未刷新", feedback)
            self.assertNotIn("保持不变", feedback)
            retry = controller.update_tm_minimum_similarity(0.80)
            self.assertFalse(retry.succeeded)
            self.assertEqual(
                retry.safe_code,
                "TM.THRESHOLD.REFRESH_FAILED",
            )
            window.close()

    def test_threshold_qt_does_not_launder_programmer_error_as_no_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller, _runtime, _composition = _canonical_controller(
                self,
                Path(temporary),
            )
            window = QtEditorWindow(controller)
            with (
                patch(
                    "qt_editor_window.prompt_tm_threshold",
                    return_value=0.80,
                ),
                patch.object(
                    controller,
                    "update_tm_minimum_similarity",
                    side_effect=TypeError("PRIVATE.PROOF.TOKEN"),
                ),
            ):
                with self.assertRaisesRegex(TypeError, "PRIVATE.PROOF.TOKEN"):
                    window._request_tm_threshold_update()
            self.assertNotIn(
                "保持不变",
                window.statusBar().currentMessage(),
            )
            window.close()

    def test_settings_exception_text_requires_typed_closed_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = ResourceRepository(Path(temporary) / "app-data")
            controller = EditorController(repository)
            dialog = QtSettingsDialog(controller)

            dialog._show_tm_action_error(RuntimeError("PRIVATE.PROOF.TOKEN"))
            self.assertNotIn("PRIVATE", dialog.status_label.text())
            self.assertIn("无法安全确认", dialog.status_label.text())

            dialog._show_tm_action_error(
                EditorControllerError("PRIVATE.PROOF.TOKEN")
            )
            self.assertNotIn("PRIVATE", dialog.status_label.text())

            dialog._show_tm_action_error(
                EditorControllerError("TM.RUNTIME.REFRESH_FAILED")
            )
            self.assertIn("运行时刷新失败", dialog.status_label.text())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
