"""A busy publication must defer UI display reads without waiting or revoking."""

from threading import Condition, Event, Thread
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import capability_host as host
from capability_frozen_inputs import _OrdinaryCapabilityInputs, _OwnerWaitGuardLock
from frozen_candidate import load_candidate
import qt_editor
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tests import test_windows_ordinary_candidate as candidate_tests
from editor_contracts import FuzzyValidationState, TMThresholdDisplay


class QtValidationPollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fixture = candidate_tests.OrdinaryCandidateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch("frozen_candidate.running_candidate", return_value=load_candidate(fixture.exe)):
            self.inputs = _OrdinaryCapabilityInputs()
        self.addCleanup(self.inputs.close)
        repository = ResourceRepository(fixture.root / "app-data")
        self.controller, self.composition = qt_editor._compose_editor_controller(repository)
        self.owner = self.composition.retrieval_gate_d_owner
        object.__setattr__(self.composition.host, "_CapabilityHost__lock", _OwnerWaitGuardLock(self.inputs))
        self.condition = Condition(_OwnerWaitGuardLock(self.inputs))
        object.__setattr__(self.owner, "_RetrievalGateDOwner__condition", self.condition)
        self.window = QtEditorWindow(self.controller)
        self.dialog = self.window.create_settings_dialog()
        self.addCleanup(self.window.close)
        self.addCleanup(self.dialog.close)

    def _busy(self, lock):
        held, release = Event(), Event()
        def hold():
            with lock:
                held.set()
                if not release.wait(5):
                    raise AssertionError("UI read waited for the publication lock")
        worker = Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(held.wait(2))
        return release, worker

    def _assert_busy_ui_defers(self, lock):
        old_window = self.window.tm_threshold_state.text()
        old_settings = self.dialog.tm_threshold_state.text()
        release, worker = self._busy(lock)
        try:
            self.window._fuzzy_validation_timer.start()
            self.window._poll_fuzzy_validation()
            self.window._refresh_tm_threshold_entry()
            self.dialog._refresh_tm_threshold_entry()
            self.assertTrue(self.window._fuzzy_validation_timer.isActive())
            self.assertEqual(self.window.tm_threshold_state.text(), old_window)
            self.assertEqual(self.dialog.tm_threshold_state.text(), old_settings)
            self.inputs._require_open()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        QTest.qWait(300)
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        self.assertFalse(self.window._fuzzy_validation_timer.isActive())
        self.assertEqual(self.window.tm_threshold_state.text(), self.dialog.tm_threshold_state.text())

    def test_ordinary_owner_busy_defers_window_poll_and_both_threshold_entries(self):
        self._assert_busy_ui_defers(self.condition)

    def test_host_busy_defers_even_when_owner_condition_is_free(self):
        self._assert_busy_ui_defers(getattr(self.composition.host, "_CapabilityHost__lock"))

    def test_controller_busy_defers_without_synchronization_or_runtime_query(self):
        self._assert_busy_ui_defers(self.controller._tm_query_lock)
        with patch.object(self.controller, "_synchronize_tm_query_state", side_effect=AssertionError("must not synchronize")):
            display = self.controller.poll_tm_threshold_display()
        self.assertIs(type(display), TMThresholdDisplay)

    def test_terminal_poll_does_not_query_and_invalid_projection_still_raises(self):
        for state, code in ((host.GateDRunState.SUCCEEDED, None), (host.GateDRunState.FAILED, "GATE_D.BENCHMARK_FAILED")):
            with self.subTest(state=state):
                object.__setattr__(self.owner, "_RetrievalGateDOwner__status", host.GateDRunStatus(1, state, code))
                self.window._fuzzy_validation_timer.start()
                with patch.object(self.window, "refresh_suggestions", side_effect=AssertionError("poll must not query")), patch.object(self.dialog, "refresh_resources", side_effect=AssertionError("poll must not synchronize")):
                    self.window._poll_fuzzy_validation()
                self.assertFalse(self.window._fuzzy_validation_timer.isActive())
                self.assertFalse(self.dialog._tm_threshold_timer.isActive())
                self.assertIs(self.window.tm_threshold_chip.property("fuzzyAvailable"), False)
                self.assertEqual(self.window.tm_threshold_state.text(), self.dialog.tm_threshold_state.text())
        with patch.object(self.controller, "poll_tm_threshold_display", return_value=object()):
            with self.assertRaises(TypeError):
                self.window._poll_fuzzy_validation()
            with self.assertRaises(TypeError):
                self.dialog._refresh_tm_threshold_entry()
        error = RuntimeError("unexpected lifecycle bug")
        with patch.object(self.controller, "poll_tm_threshold_display", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                self.window._poll_fuzzy_validation()
            self.assertIs(caught.exception, error)

    def test_projection_is_defensive_and_busy_does_not_poison_lifecycle(self):
        display = self.controller.poll_tm_threshold_display()
        self.assertIs(type(display), TMThresholdDisplay)
        object.__setattr__(display.validation, "state", FuzzyValidationState.SUCCEEDED)
        object.__setattr__(display.retrieval, "fuzzy_available", True)
        fresh = self.controller.poll_tm_threshold_display()
        self.assertIs(fresh.validation.state, FuzzyValidationState.IDLE)
        self.assertFalse(fresh.retrieval.fuzzy_available)

    def test_runtime_block_and_invalid_owner_facts_are_not_bypassed(self):
        self.controller._tm_runtime_blocked_safe_code = "TM.RUNTIME.UNAVAILABLE"
        display = self.controller.poll_tm_threshold_display()
        self.assertFalse(display.retrieval.context_available)
        self.assertFalse(display.retrieval.fuzzy_available)
        self.assertEqual(display.retrieval.safe_codes, ("TM.RUNTIME.UNAVAILABLE",))
        object.__setattr__(self.owner, "_RetrievalGateDOwner__status", object())
        with self.assertRaisesRegex(TypeError, "Gate D status"):
            self.controller.poll_tm_threshold_display()

    def test_w3_guard_also_defers_without_revoking_its_source(self):
        from capability_frozen_inputs import _test_frozen_capability_source
        from tests.test_capability_host_frozen_inputs import ROOT
        from tests.test_tm_gate_input_composition import _frozen_entries

        source = _test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        condition = Condition(_OwnerWaitGuardLock(source))
        object.__setattr__(self.owner, "_RetrievalGateDOwner__condition", condition)
        self._assert_busy_ui_defers(condition)
        source._require_open()

    def test_closing_busy_views_stops_retry_timers(self):
        self.window.show()
        self.dialog.show()
        release, worker = self._busy(self.condition)
        try:
            self.window._poll_fuzzy_validation()
            self.dialog._refresh_tm_threshold_entry()
            self.assertTrue(self.window._fuzzy_validation_timer.isActive())
            self.assertTrue(self.dialog._tm_threshold_timer.isActive())
            self.dialog.close()
            self.window.close()
            self.assertFalse(self.window._fuzzy_validation_timer.isActive())
            self.assertFalse(self.dialog._tm_threshold_timer.isActive())
        finally:
            release.set()
            worker.join(2)

    def test_queued_terminal_refresh_is_discarded_if_window_closes_before_delivery(self):
        calls = []
        with (
            patch.object(type(self.composition.matcher_validation_owner), "validate_text_v1"),
            patch.object(type(self.composition.retrieval_gate_c_validation_owner), "validate_gate_c"),
            patch.object(type(self.owner), "restore_gate_d"),
            patch.object(self.window, "refresh_suggestions", side_effect=lambda: calls.append("refresh")),
        ):
            worker = qt_editor._start_capability_validation(self.composition, self.window)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            calls.clear()
            object.__setattr__(self.owner, "_RetrievalGateDOwner__status", host.GateDRunStatus(1, host.GateDRunState.SUCCEEDED, None))
            self.window._poll_fuzzy_validation()
            self.assertEqual(calls, [], "terminal poll must only enqueue refresh")
            self.window.close()
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            self.assertEqual(calls, [], "already queued refresh must respect close")

    def test_closed_settings_timer_is_not_restarted_by_later_window_polls(self):
        self.window.show()
        self.dialog.show()
        object.__setattr__(self.owner, "_RetrievalGateDOwner__status", host.GateDRunStatus(1, host.GateDRunState.RUNNING, None))
        self.window._poll_fuzzy_validation()
        self.assertTrue(self.dialog._tm_threshold_timer.isActive())
        self.dialog.close()
        self.assertFalse(self.dialog._tm_threshold_timer.isActive())
        self.window._poll_fuzzy_validation()
        self.assertFalse(self.dialog._tm_threshold_timer.isActive())
        self.dialog.show()
        self.assertTrue(self.dialog._tm_threshold_timer.isActive())
        self.dialog.close()
        self.assertFalse(self.dialog._tm_threshold_timer.isActive())
        self.window.close()
        self.assertFalse(self.dialog._tm_threshold_timer.isActive())


if __name__ == "__main__":
    unittest.main()
