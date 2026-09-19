"""Post-TCB UI consumers; all input fixtures remain test-frozen/provisional."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread, Timer, setprofile
from types import FrameType
from typing import Any, cast
import tempfile
import time
import unittest
from unittest import mock
import sys

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

import capability_host as host
import capability_frozen_inputs as frozen
import qt_editor
from editor_controller import EditorController
from qt_editor_window import QtEditorWindow
from resource_repository import ResourceRepository
from tests.test_capability_host_frozen_inputs import ROOT, NOW, calls, entries
from tests.test_tm_gate_input_composition import _frozen_entries
from qt_owner_dispatch import create_owner_thread_dispatcher


class FrozenQtCompositionTests(unittest.TestCase):
    repository: ResourceRepository = cast(ResourceRepository, cast(object, None))

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = cast(QApplication, QApplication.instance() or QApplication([]))

    def setUp(self) -> None:
        temporary_root = ROOT / "artifacts/windows/task36a-retry/qt-temporary"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temporary_root, prefix="composition-")
        self.addCleanup(temporary.cleanup)
        self.repository = ResourceRepository(Path(temporary.name) / "data")

    def test_editor_frozen_entry_reaches_core_admission_without_source_fallback(self) -> None:
        authority = object()
        with calls() as observed, self.assertRaisesRegex(RuntimeError, "production frozen input admission"):
            qt_editor._compose_editor_controller(self.repository, trusted_source_authority=authority)
        admission = entries(observed, "tm_gate_inputs", "_compose_frozen_input_owner")
        self.assertEqual(len(admission), 1)
        self.assertIs(admission[0]["authority"], authority)
        self.assertTrue(entries(observed, "qt_owner_dispatch", "create_owner_thread_dispatcher"))
        self.assertTrue(entries(observed, "qt_owner_dispatch", "close"))
        self.assertFalse(entries(observed, "platform_source_authority", "compose_rooted_source_authority"))

    def test_editor_rejects_mixed_authorities(self) -> None:
        with self.assertRaises(TypeError):
            qt_editor._compose_editor_controller(self.repository, source_authority=object(), trusted_source_authority=object())

    def test_frozen_flag_without_handoff_does_not_enter_source_composition(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True), calls() as observed:
            with self.assertRaisesRegex(RuntimeError, "trusted source handoff"):
                qt_editor._compose_editor_controller(self.repository)
            self.assertEqual(qt_editor.main([]), 1)
        self.assertFalse(entries(observed, "platform_source_authority", "compose_rooted_source_authority"))

    def _cancel_active_validation(self, action: str) -> None:
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        window = QtEditorWindow(EditorController(self.repository))
        self.addCleanup(lambda: window.deleteLater() if isValid(window) else None)
        entered, release, finished = Event(), Event(), Event()
        gate_c_calls: list[str] = []
        def observe(frame: FrameType, event: str, arg: object) -> None:
            if event == "call" and frame.f_code.co_name == "build_validated_matcher_v1":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test coordination timeout")
            if event == "call" and frame.f_code.co_name == "_recompute_retrieval_validation_from_session":
                gate_c_calls.append("called")
            if event == "return" and frame.f_code.co_name == "validate" and frame.f_globals.get("__name__") == "qt_editor":
                finished.set()
        setprofile(observe)
        try:
            worker = cast(Thread, qt_editor._start_capability_validation(composition, window))
            self.assertTrue(entered.wait(3), "actual Matcher caller was not entered")
            if action == "close":
                window.close()
            elif action == "destroy":
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            else:
                cast(Any, worker).cancel()
            with self.assertRaises(RuntimeError):
                source._require_open()
        finally:
            release.set()
            setprofile(None)
        deadline = time.monotonic() + 3
        while not finished.is_set() and time.monotonic() < deadline:
            self.app.processEvents()
            finished.wait(0.002)
        self.assertTrue(finished.is_set(), "cancelled validation was not awakened")
        worker.join(0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(gate_c_calls)
        self.assertIsNone(composition.host.matcher_snapshot().matcher)

    def test_window_close_revokes_actual_validation(self) -> None:
        self._cancel_active_validation("close")

    def test_window_destruction_revokes_actual_validation(self) -> None:
        self._cancel_active_validation("destroy")

    def test_explicit_cancel_revokes_actual_validation(self) -> None:
        self._cancel_active_validation("cancel")

    def _assert_ui_lock_does_not_wait(self, target: str) -> None:
        dispatch = create_owner_thread_dispatcher()
        self.addCleanup(dispatch.close)
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT, _scheduler=dispatch)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        if target == "host":
            lock = getattr(composition.host, "_CapabilityHost__lock")
            read = composition.host.retrieval_snapshot
        else:
            owner = composition.retrieval_gate_d_owner
            assert owner is not None
            lock = getattr(owner, "_RetrievalGateDOwner__condition")
            read = owner.status
        queued, done, second_done, watchdog_fired = Event(), Event(), Event(), Event()
        from PySide6.QtCore import Qt
        dispatch.requested.connect(queued.set, Qt.ConnectionType.DirectConnection)
        results: list[BaseException] = []
        with dispatch.window() as window:
            def wait_for_owner() -> None:
                try:
                    # This is the exact Host lock used around terminal Gate D
                    # publication; no Host method/authority is replaced.
                    with lock:
                        dispatch.call(window, lambda: None)
                except BaseException as error:
                    results.append(error)
                finally:
                    done.set()
            thread = Thread(target=wait_for_owner, daemon=True)
            thread.start()
            self.assertTrue(queued.wait(3))
            queued.clear()
            def second_waiter() -> None:
                try:
                    dispatch.call(window, lambda: None)
                except BaseException as error:
                    results.append(error)
                finally:
                    second_done.set()
            second = Thread(target=second_waiter, daemon=True)
            second.start()
            self.assertTrue(queued.wait(3))
            def rescue() -> None:
                watchdog_fired.set()
                dispatch.close()
            watchdog = Timer(1, rescue)
            watchdog.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "cannot block"):
                    read()
            finally:
                watchdog.cancel()
            self.assertTrue(done.wait(3), "Host read left native waiter blocked")
            self.assertTrue(second_done.wait(3), "second native waiter was not awakened")
            thread.join(0)
            second.join(0)
        self.assertFalse(watchdog_fired.is_set(), "UI waited until watchdog cancellation")
        self.assertEqual(len(results), 2)
        with self.assertRaises(RuntimeError):
            source._require_open()

    def test_ui_host_read_cannot_wait_on_worker_holding_host_lock(self) -> None:
        self._assert_ui_lock_does_not_wait("host")

    def test_ui_gate_d_status_cannot_wait_on_worker_holding_publication_lock(self) -> None:
        self._assert_ui_lock_does_not_wait("gate-d")

    def test_ui_generation_wait_revokes_pending_requests(self) -> None:
        dispatch = create_owner_thread_dispatcher()
        self.addCleanup(dispatch.close)
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT, _scheduler=dispatch)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        with self.assertRaisesRegex(RuntimeError, "cannot block"):
            composition.host.retrieval_generation_notifications().wait_for_change(after_generation=0, timeout=1)
        with self.assertRaises(RuntimeError):
            source._require_open()

    def test_actual_validation_thread_join_on_ui_revokes_native_wait(self) -> None:
        from PySide6.QtCore import Qt
        dispatch = create_owner_thread_dispatcher()
        self.addCleanup(dispatch.close)
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT, _scheduler=dispatch)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        queued = Event()
        dispatch.requested.connect(queued.set, Qt.ConnectionType.DirectConnection)
        with dispatch.window() as window:
            def observe(frame: FrameType, event: str, arg: object) -> None:
                if event == "call" and frame.f_code.co_name == "build_validated_matcher_v1":
                    dispatch.call(window, lambda: None)
            setprofile(observe)
            try:
                worker = cast(Thread, qt_editor._start_capability_validation(composition))
                self.assertTrue(queued.wait(3))
                with self.assertRaisesRegex(RuntimeError, "cannot block"):
                    worker.join(1)
            finally:
                setprofile(None)
                dispatch.close()
            deadline = time.monotonic() + 3
            while worker.is_alive() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.002)
            self.assertFalse(worker.is_alive(), "actual validation join left worker blocked")
        with self.assertRaises(RuntimeError):
            source._require_open()

    def test_worker_condition_restores_after_source_cancellation(self) -> None:
        dispatch = create_owner_thread_dispatcher()
        self.addCleanup(dispatch.close)
        source = frozen._test_frozen_capability_source(_frozen_entries(), ROOT, _scheduler=dispatch)
        self.addCleanup(source.close)
        composition = host.compose_capability_host(source_authority=source, evaluated_at_utc=NOW)
        started, done = Event(), Event()
        result: list[object] = []
        def wait_generation() -> None:
            try:
                started.set()
                result.append(composition.host.retrieval_generation_notifications().wait_for_change(after_generation=0, timeout=0.05))
            except BaseException as error:
                result.append(error)
            finally:
                done.set()
        thread = Thread(target=wait_generation, daemon=True)
        thread.start()
        self.assertTrue(started.wait(3))
        source.close()
        self.assertTrue(done.wait(3))
        thread.join(0)
        self.assertEqual(result, [None])
