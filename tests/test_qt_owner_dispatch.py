"""Real Qt queued calls and revocation, without native/frozen admission."""

from __future__ import annotations

from threading import Event, Thread, get_ident
from typing import Callable, cast
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from qt_owner_dispatch import _OwnerThreadDispatcher, create_owner_thread_dispatcher


class OwnerDispatchTests(unittest.TestCase):
    dispatch: _OwnerThreadDispatcher = cast(_OwnerThreadDispatcher, cast(object, None))

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.dispatch = create_owner_thread_dispatcher()
        self.addCleanup(self.dispatch.close)

    def launch(self, operation: Callable[[], object]):
        queued = Event()
        self.dispatch.requested.connect(queued.set, Qt.ConnectionType.DirectConnection)
        done, result = Event(), []
        def run() -> None:
            try:
                result.append(operation())
            except BaseException as error:
                result.append(error)
            finally:
                done.set()
        thread = Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(queued.wait(3), "worker never queued its request")
        return thread, done, result

    def finish(self, launched, *, pump=True):
        thread, done, result = launched
        deadline = time.monotonic() + 3
        while not done.is_set() and time.monotonic() < deadline:
            if pump:
                self.app.processEvents()
            done.wait(0.002)
        self.assertTrue(done.is_set(), "waiting worker was not awakened")
        thread.join(0)
        self.assertEqual(len(result), 1)
        return result[0]

    def test_worker_call_runs_on_original_qt_thread(self):
        with self.dispatch.window() as window:
            result = self.finish(self.launch(lambda: self.dispatch.call(window, get_ident)))
            self.assertEqual(result, get_ident())

    def test_cancel_wakes_waiter_and_old_queued_call_never_executes(self):
        calls = []
        with self.dispatch.window() as window:
            job = self.launch(lambda: self.dispatch.call(window, lambda: calls.append("stale")))
            self.dispatch.cancel(window)
            self.assertIsInstance(self.finish(job, pump=False), RuntimeError)
        self.app.processEvents()
        self.assertEqual(calls, [])
        with self.dispatch.window() as current:
            self.assertEqual(self.dispatch.call(current, lambda: 7), 7)
            with self.assertRaises(RuntimeError):
                self.dispatch.call(window, lambda: 8)

    def test_close_wakes_waiter_without_owner_waiting_for_worker(self):
        calls = []
        with self.dispatch.window() as window:
            job = self.launch(lambda: self.dispatch.call(window, lambda: calls.append(1)))
            self.dispatch.close()
            self.assertIsInstance(self.finish(job, pump=False), RuntimeError)
        self.app.processEvents()
        self.assertFalse(calls)
        with self.assertRaises(RuntimeError):
            with self.dispatch.window():
                pass

    def test_callback_error_revokes_window_and_wakes_waiters(self):
        def fail():
            raise ValueError("test callback")
        with self.dispatch.window() as window:
            result = self.finish(self.launch(lambda: self.dispatch.call(window, fail)))
            self.assertIsInstance(result, ValueError)
            with self.assertRaises(RuntimeError):
                self.dispatch.call(window, lambda: 1)

    def test_reentry_revokes_outer_request_even_if_callback_catches_it(self):
        with self.dispatch.window() as window:
            def nested():
                with self.assertRaises(RuntimeError):
                    self.dispatch.call(window, lambda: 2)
                return 1
            self.assertIsInstance(self.finish(self.launch(lambda: self.dispatch.call(window, nested))), RuntimeError)

    def test_owner_blocking_wait_is_rejected_and_revokes_current_window(self):
        with self.dispatch.window() as window:
            job = self.launch(lambda: self.dispatch.call(window, lambda: 1))
            with self.assertRaises(RuntimeError):
                self.dispatch.require_worker_wait_allowed()
            self.assertIsInstance(self.finish(job, pump=False), RuntimeError)

    def test_qobject_destruction_wakes_an_already_queued_waiter(self):
        calls = []
        with self.dispatch.window() as window:
            job = self.launch(lambda: self.dispatch.call(window, lambda: calls.append(1)))
            self.dispatch.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertIsInstance(self.finish(job, pump=False), RuntimeError)
        self.assertFalse(calls)

    def test_first_callback_error_revokes_a_second_queued_waiter(self):
        def fail():
            raise ValueError("first request")
        calls = []
        with self.dispatch.window() as window:
            first = self.launch(lambda: self.dispatch.call(window, fail))
            second = self.launch(lambda: self.dispatch.call(window, lambda: calls.append(1)))
            self.assertIsInstance(self.finish(first), ValueError)
            self.assertIsInstance(self.finish(second, pump=False), RuntimeError)
        self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
