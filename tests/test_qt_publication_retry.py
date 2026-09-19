"""Real Qt queued terminal and Host/Core cancellation, provisional inputs only."""
from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Thread, get_ident
import time
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import capability_frozen_inputs as frozen
import tm_gate_inputs as inputs
from qt_owner_dispatch import create_owner_thread_dispatcher
from tests import test_capability_publication_retry as probes


class QtPublicationRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def check_close(self, *, after_terminal: bool):
        harness = probes.PublicationRetryTests()
        dispatch = create_owner_thread_dispatcher()
        self.addCleanup(dispatch.close)
        source = frozen._test_frozen_capability_source((("probe", b"retained"),), probes.ROOT, _scheduler=dispatch)
        self.addCleanup(source.close)
        subject, graph, owner, receipt = harness.fixture()
        prior = harness.state(subject, graph, owner)
        queued, terminal_done, resume, done, second_done = Event(), Event(), Event(), Event(), Event()
        dispatch.requested.connect(queued.set, Qt.ConnectionType.DirectConnection)
        errors, native_threads, references = [], [], []
        original = inputs._TestFrozenInputAdapter.proof_window
        with dispatch.window() as window:
            @contextmanager
            def proof_window(adapter):
                with original(adapter):
                    yield
                    dispatch.call(window, lambda: native_threads.append(get_ident()))
            def pause_after_terminal(session):
                terminal_done.set()
                if not resume.wait(3):
                    raise AssertionError("terminal worker pause deadline")
            def worker():
                try:
                    harness.publish(owner, graph, receipt)
                except BaseException as error:
                    errors.append(error)
                finally:
                    done.set()
            def before_prepare(session):
                references.append(session.input("probe"))
            with harness.provisional_publication(source, after_prepare=before_prepare, after_terminal=pause_after_terminal if after_terminal else None), mock.patch.object(inputs._TestFrozenInputAdapter, "proof_window", proof_window):
                thread = Thread(target=worker, daemon=True)
                thread.start()
                self.assertTrue(queued.wait(3))
                if after_terminal:
                    deadline = time.monotonic() + 3
                    while not terminal_done.is_set() and time.monotonic() < deadline:
                        self.app.processEvents()
                        terminal_done.wait(0.001)
                    self.assertTrue(terminal_done.is_set())
                queued.clear()
                def second_waiter():
                    try:
                        dispatch.call(window, lambda: self.fail("stale native request ran"))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        second_done.set()
                second = Thread(target=second_waiter, daemon=True)
                second.start()
                self.assertTrue(queued.wait(3))
                try:
                    # No event pumping or worker completion is needed for close.
                    source.close()
                    self.assertTrue(second_done.wait(3))
                    if after_terminal:
                        self.assertFalse(done.is_set())
                finally:
                    resume.set()
                self.assertTrue(done.wait(3))
                thread.join(0)
                second.join(0)
            self.app.processEvents()
            with self.assertRaises(RuntimeError):
                dispatch.call(window, lambda: None)
        self.assertEqual(native_threads, [get_ident()] if after_terminal else [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
        self.assertTrue(all(left is right for left, right in zip(harness.state(subject, graph, owner), prior)))
        self.assertEqual(len(references), 1)
        with self.assertRaises(RuntimeError):
            references[0]._read()

    def test_ui_close_while_terminal_queued_revokes_owner_and_wakes_all_waiters(self):
        self.check_close(after_terminal=False)

    def test_ui_close_after_terminal_returns_without_waiting_for_paused_worker(self):
        self.check_close(after_terminal=True)


if __name__ == "__main__":
    unittest.main()
