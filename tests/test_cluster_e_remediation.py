"""Cluster E cumulative-review remediation journeys."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread, current_thread
import time
import types
from typing import cast
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

import capability_host as capability_host_module
from capability_host import CapabilityHostComposition
from editor_contracts import (
    EditorProject,
    EditorSegment,
    ResourceKind,
    RetrievalDisplayState,
    SuggestionBundle,
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
from tm_retrieval_capability import (
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
)


def _live_gate_d_composition(
    test_case: unittest.TestCase,
    execution: _FakeGateDExecution,
) -> CapabilityHostComposition:
    """Attach the fake Gate D runner to a composition evaluated at now."""

    composition = capability_host_module.compose_capability_host(
        evaluated_at_utc=datetime.now(timezone.utc),
    )
    owner = composition.retrieval_gate_d_owner
    self_owner = owner
    assert self_owner is not None
    object.__setattr__(
        self_owner,
        "_RetrievalGateDOwner__execute",
        execution.run,
    )

    def publish_from_authentic_binding(
        binding: object,
        *,
        run_result: object,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        evaluated_at_utc: datetime,
        prepare_publication: object,
    ) -> object:
        del run_result
        if not any(binding is current for current in execution.bindings):
            raise AssertionError("unexpected Gate D binding")
        return execution.publish(
            base_manifest=base_manifest,
            publisher=publisher,
            evaluated_at_utc=evaluated_at_utc,
            prepare_publication=prepare_publication,
        )

    binding_type = getattr(
        capability_host_module,
        "_CoreGateDBinding",
    )
    binding_patch = patch.object(
        binding_type,
        "publish",
        publish_from_authentic_binding,
    )
    binding_patch.start()
    test_case.addCleanup(binding_patch.stop)
    return composition


class ClusterERemediationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    def _wait_for(self, predicate: object, *, timeout: float = 10.0) -> None:
        if not callable(predicate):
            raise TypeError("wait predicate must be callable")
        deadline = time.monotonic() + timeout
        while not predicate():
            self._events()
            if time.monotonic() >= deadline:
                self.fail("timed out waiting for asynchronous Qt state")
            time.sleep(0.01)
        self._events()

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

    def test_capability_completion_refreshes_window_at_gate_c_and_gate_d(
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
            gate_d_release = Event()
            execution = _FakeGateDExecution(release=gate_d_release)
            composition = _live_gate_d_composition(self, execution)
            controller = EditorController(
                repository,
                tm_adapter=EditorTMAdapter(
                    runtime_host=runtime,
                    capability_host=composition.host,
                ),
            )
            controller.set_project(
                EditorProject(
                    name="Capability completion",
                    segments=(EditorSegment(id="one", source="aabba"),),
                )
            )
            refresh_threads: list[Thread] = []

            class TrackingWindow(QtEditorWindow):
                def refresh_suggestions(self) -> SuggestionBundle:
                    refresh_threads.append(current_thread())
                    return super().refresh_suggestions()

            window = TrackingWindow(controller)
            initial_report = window.current_tm_report
            self.assertIsNotNone(initial_report)
            assert initial_report is not None
            self.assertFalse(initial_report.retrieval_status.context_available)
            self.assertFalse(initial_report.retrieval_status.fuzzy_available)
            self.assertIs(
                window.tm_threshold_chip.property("fuzzyAvailable"),
                False,
            )
            initial_epoch = initial_report.query_identity.query_epoch
            refresh_threads.clear()

            worker = cast(
                Thread,
                qt_editor._start_capability_validation(
                    composition,
                    window.refresh_suggestions,
                ),
            )
            self.assertTrue(worker.daemon)
            self.assertTrue(execution.started.wait(10.0))
            self.assertTrue(worker.is_alive())
            self._wait_for(lambda: len(refresh_threads) == 1)

            gate_c_report = window.current_tm_report
            self.assertIsNotNone(gate_c_report)
            assert gate_c_report is not None
            self.assertTrue(gate_c_report.retrieval_status.context_available)
            self.assertFalse(gate_c_report.retrieval_status.fuzzy_available)
            self.assertIs(
                window.tm_threshold_chip.property("fuzzyAvailable"),
                False,
            )
            self.assertEqual(
                gate_c_report.query_identity.query_epoch,
                initial_epoch + 1,
            )

            gate_d_release.set()
            self._wait_for(lambda: len(refresh_threads) == 2)
            worker.join(10.0)
            self.assertFalse(worker.is_alive())
            gate_d_report = window.current_tm_report
            self.assertIsNotNone(gate_d_report)
            assert gate_d_report is not None
            self.assertTrue(gate_d_report.retrieval_status.context_available)
            self.assertTrue(gate_d_report.retrieval_status.fuzzy_available)
            self.assertIs(
                window.tm_threshold_chip.property("fuzzyAvailable"),
                True,
            )
            self.assertEqual(
                gate_d_report.query_identity.query_epoch,
                initial_epoch + 2,
            )
            self.assertTrue(
                all(thread is current_thread() for thread in refresh_threads)
            )
            window.close()

    def test_capability_completion_ignores_destroyed_window(self) -> None:
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
            gate_d_release = Event()
            execution = _FakeGateDExecution(release=gate_d_release)
            composition = _live_gate_d_composition(self, execution)
            controller = EditorController(
                repository,
                tm_adapter=EditorTMAdapter(
                    runtime_host=runtime,
                    capability_host=composition.host,
                ),
            )
            controller.set_project(
                EditorProject(
                    name="Destroyed completion receiver",
                    segments=(EditorSegment(id="one", source="aabba"),),
                )
            )
            refresh_threads: list[Thread] = []

            class TrackingWindow(QtEditorWindow):
                def refresh_suggestions(self) -> SuggestionBundle:
                    refresh_threads.append(current_thread())
                    return super().refresh_suggestions()

            window = TrackingWindow(controller)
            refresh_threads.clear()
            worker = cast(
                Thread,
                qt_editor._start_capability_validation(
                    composition,
                    window.refresh_suggestions,
                ),
            )
            self.assertTrue(execution.started.wait(10.0))
            self._wait_for(lambda: len(refresh_threads) == 1)
            destroyed = Event()
            window.destroyed.connect(lambda: destroyed.set())
            window.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose,
                True,
            )
            window.close()
            QCoreApplication.sendPostedEvents(
                None,
                QEvent.Type.DeferredDelete,
            )
            self._events()
            self.assertTrue(destroyed.is_set())
            calls_before_gate_d = len(refresh_threads)

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                gate_d_release.set()
                worker.join(10.0)
                self.assertFalse(worker.is_alive())
                self._events()
            self.assertEqual(len(refresh_threads), calls_before_gate_d)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("Exception in thread", stderr.getvalue())

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
