"""GUI polling bridge for an already-issued, Qt-free file job."""
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal


class QtProjectFileJob(QObject):
    finished = Signal()

    def __init__(self, job, deliver, *, parent=None):
        super().__init__(parent)
        self.job = job
        self._deliver = deliver
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._poll)
        self._thread = None

    def start(self):
        # Capture the plain job only. Even after this QObject is deleted, the
        # worker can dispose its candidate without touching Qt or Controller.
        job = self.job
        self._thread = Thread(target=job.run, name='localcat-project-file', daemon=False)
        try:
            self._thread.start()
        except Exception as error:
            job.fail_before_start(error)
        self._timer.start()

    def cancel(self):
        self.job.cancel()

    def close(self):
        self._closed = True
        self._timer.stop()
        self.job.dispose()
        self._deliver = None
        self.finished.emit()
        self.deleteLater()

    def _poll(self):
        if self._closed or not self.job.done:
            return
        self._timer.stop()
        try:
            self._deliver(self.job)
        finally:
            self._deliver = None
            self.finished.emit()
            self.deleteLater()
