"""Startup work is single-owner and only complete results reach Qt."""

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from qt_startup_loader import QtStartupLoader


class QtStartupLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_blocked_preload_keeps_event_loop_live_and_publishes_once_on_gui(self):
        entered, release = threading.Event(), threading.Event()
        workers, published, failures = [], [], []
        result = object()

        def build():
            workers.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return result

        loader = QtStartupLoader(
            build, lambda value: published.append((value, threading.get_ident())),
            failures.append,
        )
        try:
            loader.start()
            self.wait_until(entered.is_set)
            loader.start()
            QTest.qWait(60)
            self.assertEqual(published, [])
            self.assertEqual(len(workers), 1)
            self.assertNotEqual(workers[0], threading.get_ident())
            release.set()
            self.wait_until(lambda: bool(published))
            self.assertEqual(published, [(result, threading.get_ident())])
            self.assertFalse(loader.running)
            self.assertEqual(failures, [])
        finally:
            release.set()
            loader.stop_publication()
            loader._thread.join(3)

    def test_failed_build_can_retry_without_publishing_partial_owner(self):
        attempts, published, failures = [], [], []
        error = ValueError("fixture-private-detail")

        def build():
            attempts.append(1)
            if len(attempts) == 1:
                raise error
            return "complete"

        loader = QtStartupLoader(build, published.append, failures.append)
        try:
            loader.start()
            self.wait_until(lambda: bool(failures))
            self.assertEqual(published, [])
            self.assertEqual(failures, [error])
            loader.start()
            self.wait_until(lambda: bool(published))
            self.assertEqual(published, ["complete"])
        finally:
            loader.stop_publication()
            loader._thread.join(3)

    def test_close_discards_result_without_interrupting_core_work(self):
        entered, release, finished = (threading.Event() for _ in range(3))
        published, failures = [], []

        def build():
            entered.set()
            release.wait(3)
            finished.set()
            return object()

        loader = QtStartupLoader(build, published.append, failures.append)
        try:
            loader.start()
            self.wait_until(entered.is_set)
            loader.stop_publication()
            self.assertFalse(loader._thread.daemon)
            release.set()
            self.wait_until(finished.is_set)
            QTest.qWait(60)
            self.assertEqual(published, [])
            self.assertEqual(failures, [])
            loader.start()
            self.assertTrue(loader._closed)
        finally:
            release.set()
            loader._thread.join(3)

    def test_gui_publication_failure_uses_failure_boundary(self):
        failures = []

        def publish(_result):
            raise ValueError("fixture-gui-detail")

        loader = QtStartupLoader(lambda: object(), publish, failures.append)
        try:
            loader.start()
            self.wait_until(lambda: bool(failures))
            self.assertIsInstance(failures[0], ValueError)
            self.assertFalse(loader.running)
        finally:
            loader.stop_publication()
            loader._thread.join(3)

    def test_thread_start_failure_restores_retryable_state(self):
        published, failures = [], []
        loader = QtStartupLoader(lambda: "ready", published.append, failures.append)
        try:
            with patch("qt_startup_loader.Thread.start", side_effect=RuntimeError("fixture")):
                loader.start()
            self.assertFalse(loader.running)
            self.assertFalse(loader._timer.isActive())
            self.assertEqual(len(failures), 1)
            self.assertEqual(published, [])
            loader.start()
            self.wait_until(lambda: bool(published))
            self.assertEqual(published, ["ready"])
        finally:
            loader.stop_publication()
            if loader._thread is not None:
                loader._thread.join(3)
