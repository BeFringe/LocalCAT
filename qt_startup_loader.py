"""Single-owner background preload with GUI-thread-only publication."""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, SimpleQueue
from threading import Thread

from PySide6.QtCore import QObject, QTimer


class QtStartupLoader(QObject):
    """Build plain Python owners off-thread; never expose a partial result.

    Closing the home stops publication, not the Core operation. The non-daemon
    worker is allowed to close its own reservations normally before process
    exit. No QObject, widget, or persistent SQLite connection may be built by
    ``build``; the returned owner graph is handed off only after it finishes.
    """

    def __init__(
        self,
        build: Callable[[], object],
        publish: Callable[[object], None],
        failed: Callable[[Exception], None],
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._build = build
        self._publish = publish
        self._failed = failed
        self._results: SimpleQueue[tuple[bool, object]] = SimpleQueue()
        self._thread: Thread | None = None
        self._closed = False
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._closed or self._running:
            return
        self._running = True
        self._timer.start()
        # Capture only the builder and stdlib queue. The worker never touches
        # this QObject, including after its window/application has closed.
        build, results = self._build, self._results

        def run() -> None:
            try:
                result = build()
            except Exception as error:
                results.put((False, error))
            else:
                results.put((True, result))

        self._thread = Thread(
            target=run, name="localcat-resource-preload", daemon=False
        )
        try:
            self._thread.start()
        except Exception as error:
            self._thread = None
            self._running = False
            self._timer.stop()
            self._failed(error)

    def stop_publication(self) -> None:
        self._closed = True
        self._timer.stop()

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            succeeded, result = self._results.get_nowait()
        except Empty:
            return
        self._running = False
        self._timer.stop()
        if succeeded:
            try:
                self._publish(result)
            except Exception as error:
                self._failed(error)
        else:
            assert isinstance(result, Exception)
            self._failed(result)
