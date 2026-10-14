"""Task 10.2 Qt outcomes over real stopped resource-family owners."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, Thread
from typing import cast
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel

from capability_host import CapabilityHostComposition
from editor_contracts import FuzzyValidationState, ResourceKind, TMResourceDisplayMode
from editor_controller import EditorController, EditorControllerError
from editor_project import load_project
from editor_tm_adapter import EditorTMAdapter
import qt_editor
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tests.test_capability_host_gate_d import (
    _EVALUATED_AT,
    _FakeGateDExecution,
    _composition,
    _gate_c,
)
from tests.test_editor_controller_tm_activation_completion import _activation_test_root
from tests.test_ui_resource_session_failure_matrix import _fixture, _replace_directory
from tm_application_composition import TMRuntimeHost, _binding_owner


@unittest.skipUnless(os.name == "posix", "POSIX observable configured-directory replacement")
class QtResourceStopOutcomesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _events() -> None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    @staticmethod
    def _close(window: QtEditorWindow) -> None:
        # Only cleanup bypasses the unsaved-close prompt after a failed assertion.
        with patch.object(window, "_confirm_unsaved", return_value=True):
            window.close()

    def _window(
        self, root: Path, composition: CapabilityHostComposition | None = None,
    ) -> tuple[QtEditorWindow, TMRuntimeHost, ResourceRepository, str]:
        controller, _adapter, runtime, repository, peer_id = _fixture(root)
        if composition is not None:
            # Reuse the same resolver/owners while selecting the established
            # lightweight capability fixture and production display projection.
            adapter = EditorTMAdapter(
                runtime_host=runtime,
                capability_host=composition.host,
                fuzzy_validation_status=lambda: qt_editor._fuzzy_validation_display(composition),
                fuzzy_validation_poll=lambda: qt_editor._poll_fuzzy_validation_display(composition),
            )
            original_project = controller.project
            controller = EditorController(repository, tm_adapter=adapter)
            controller.set_project(original_project)
        controller.create_resource("Healthy terms", ResourceKind.TERMBASE)
        controller.add_term("Hello", "term target")
        operation = controller.activate_tm_resource(controller.prepare_tm_activation("local-tm"))
        self.assertTrue(controller.wait_tm_activation(operation.operation_id, timeout=20.0).succeeded)
        # Exercise the combined ResourcePort against the same published family.
        exported = controller.export_resource_package("local-tm", root / "tm.localcat-resource")
        self.assertEqual(exported.receipt.record_count, 1)
        window = QtEditorWindow(controller)
        self.addCleanup(self._close, window)
        window.show()
        self._events()
        return window, runtime, repository, peer_id

    def _assert_stopped_projection(
        self, window: QtEditorWindow, repository: ResourceRepository, peer_id: str,
    ) -> None:
        self._events()
        report = window.current_tm_report
        assert report is not None
        self.assertEqual({item.resource_id for item in report.suggestions}, {peer_id})
        statuses = {item.resource_id: item for item in report.resource_statuses}
        stopped = statuses["local-tm"]
        self.assertEqual(stopped.mode, TMResourceDisplayMode.UNAVAILABLE)
        self.assertFalse(stopped.exact_available or stopped.context_available or stopped.fuzzy_available)
        self.assertNotIn("TM.RUNTIME.CANONICAL_REATTESTATION_REQUIRED", stopped.safe_codes)
        self.assertTrue(statuses[peer_id].exact_available)
        self.assertEqual([item.target_term for item in window.controller.term_suggestions()], ["term target"])
        state = window.findChild(QLabel, "tmSuggestionState")
        assert state is not None
        self.assertIn("资源当前不可用", state.text())
        self.assertIn(repository.get("local-tm").name, state.text())
        self.assertNotIn("暂无", state.text())
        projection = repr(report) + state.text()
        for private in (str(repository.get("local-tm").path.parent), ".sqlite3", "binding_digest", "retained_root"):
            self.assertNotIn(private, projection)

    def test_tm_write_failure_does_not_confirm_navigate_or_prevent_project_save(self) -> None:
        with _activation_test_root() as raw:
            root = raw.resolve()
            window, _runtime, repository, peer_id = self._window(root)
            controller = window.controller
            _replace_directory(repository.get("local-tm").path.parent)
            controller.reload_resources()
            window.refresh_suggestions()
            self._assert_stopped_projection(window, repository, peer_id)

            dialog = window.create_settings_dialog()
            self.addCleanup(dialog.close)
            action = dialog.findChild(QAction, "tmLifecycleAction_local-tm")
            assert action is not None
            self.assertFalse(action.isEnabled())
            self.assertNotIn("重新验证", action.text())
            with self.assertRaises(EditorControllerError):
                controller.reattest_tm_resource("local-tm")

            self.assertTrue(window.target_editor.isEnabled())
            self.assertTrue(window.save_button.isEnabled())
            window.target_editor.setPlainText("independent saved target")
            self._events()
            before_index = controller.current_index
            with patch.object(window, "_show_error") as show_error:
                window.confirm_button.click()
                self._events()
            show_error.assert_called_once()
            title, message = show_error.call_args.args
            self.assertEqual(title, "记忆库写入失败")
            self.assertEqual(message, next(
                status.safe_codes[0]
                for status in _runtime.snapshot().statuses
                if status.resource_id == "local-tm"
            ))
            self.assertNotIn(str(repository.get("local-tm").path.parent), message)
            self.assertNotIn("proof", message.lower())
            self.assertEqual(window.statusBar().currentMessage(), "记忆库写入失败；当前段未确认。")
            self.assertFalse(controller.current_segment.confirmed)
            self.assertEqual(controller.current_index, before_index)
            self.assertTrue(controller.dirty)
            peer_rows = [json.loads(line) for line in repository.get(peer_id).path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(peer_rows[-1]["target"], "independent saved target")

            saved_path = root / "project.json"
            self.assertTrue(window.save_project_path(saved_path))
            self.assertIn("已保存", window.statusBar().currentMessage())
            persisted = load_project(saved_path)
            self.assertEqual(persisted.segments[0].target, "independent saved target")
            self.assertFalse(persisted.segments[0].confirmed)
            self.assertEqual(controller.current_index, before_index)
            self.assertFalse(controller.dirty)
            window.refresh_suggestions()
            self._assert_stopped_projection(window, repository, peer_id)

    def test_queued_fuzzy_completion_cannot_revive_stopped_owner(self) -> None:
        with _activation_test_root() as raw:
            release = Event()
            execution = _FakeGateDExecution(release=release)
            composition = _composition(self, execution)
            window, runtime, repository, peer_id = self._window(raw.resolve(), composition)
            controller = window.controller
            owner = composition.retrieval_gate_d_owner
            assert owner is not None
            resolver = controller._tm_resource_resolver
            original_owner = _binding_owner(resolver._bindings["local-tm"][1])
            with patch.object(window, "refresh_suggestions", wraps=window.refresh_suggestions) as refresh:
                worker = cast(Thread, qt_editor._start_capability_validation(composition, window))
                worker.join(20.0)
                self.assertFalse(worker.is_alive())
                self._events()
                # Restore the fixture's fixed clock after the real startup bridge.
                _gate_c(composition)
                window.refresh_suggestions()
                issued = next(item for item in controller.issued_tm_suggestions if item.resource_id == "local-tm")
                before_generation = composition.host.retrieval_snapshot().generation
                try:
                    owner.start_gate_d(evaluated_at_utc=_EVALUATED_AT)
                    self.assertTrue(execution.started.wait(5.0))
                    self.assertIs(controller.tm_fuzzy_validation_status().state, FuzzyValidationState.RUNNING)
                    _replace_directory(repository.get("local-tm").path.parent)
                    controller.reload_resources()
                    window.refresh_suggestions()
                    self._assert_stopped_projection(window, repository, peer_id)
                finally:
                    release.set()
                completed = owner.wait(timeout=10.0)
                self.assertEqual(completed.state.value, "SUCCEEDED")
                self.assertGreater(composition.host.retrieval_snapshot().generation, before_generation)
                calls_before_delivery = refresh.call_count
                window._poll_fuzzy_validation()
                self.assertEqual(refresh.call_count, calls_before_delivery)
                self._events()
                self.assertGreater(refresh.call_count, calls_before_delivery)
                self.assertIs(controller.tm_fuzzy_validation_status().state, FuzzyValidationState.SUCCEEDED)
                self._assert_stopped_projection(window, repository, peer_id)
                self.assertIs(_binding_owner(resolver._bindings["local-tm"][1]), original_owner)
                self.assertNotIn("local-tm", {port.resource_id for port in runtime.snapshot().canonical_ports})
                with self.assertRaises(EditorControllerError):
                    controller.apply_tm_suggestion(issued)
                controller.reload_resources()
                window._capability_display_refresh_requested.emit()
                self._events()
                self._assert_stopped_projection(window, repository, peer_id)
                self.assertIs(_binding_owner(resolver._bindings["local-tm"][1]), original_owner)


if __name__ == "__main__":
    unittest.main()
