"""Single-file export UI over Controller-issued display projections only."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from editor_controller import EditorControllerError
from qt_project_file_job import QtProjectFileJob


class QtProjectExportDialog(QDialog):
    activity_changed = Signal()
    result_ready = Signal(object)
    segment_located = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.view = None
        self.result = None
        self._target = None
        self._runner = None
        self._shut_down = False
        self.setWindowTitle('导出 Ren’Py TL')
        self.resize(660, 420)
        layout = QVBoxLayout(self)
        self.target_label = QLabel('请选择完整的导出文件路径。')
        self.summary_label = QLabel('预览将使用全部当前译文；空译文保持为空。')
        self.status_label = QLabel('导出不会保存项目包，未保存修改仍需另行保存。')
        for label in (self.target_label, self.summary_label, self.status_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        layout.addWidget(self.diagnostics)
        self.locate_button = QPushButton('定位首个诊断段落')
        self.locate_button.clicked.connect(self._locate_diagnostic)
        layout.addWidget(self.locate_button)
        buttons = QHBoxLayout()
        self.target_button = QPushButton('选择目标并重新预览…')
        self.confirm_button = QPushButton('确认导出')
        self.cancel_button = QPushButton('取消操作')
        self.close_button = QPushButton('关闭')
        for button in (self.target_button, self.confirm_button, self.cancel_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.target_button.clicked.connect(self.select_target)
        self.confirm_button.clicked.connect(self.confirm_export)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.close_button.clicked.connect(self.close)
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._watch_context)
        self._watch.start()
        self._update_access()

    @property
    def operation_running(self):
        return self._runner is not None

    def select_target(self, _checked=False, *, initial=None):
        if self.operation_running or self._shut_down:
            return
        initial = initial or self._target or self.controller.tl_export_default_path
        if initial is None:
            self.status_label.setText('当前项目不能导出 TL。')
            return
        selected, _ = QFileDialog.getSaveFileName(
            self, '导出 Ren’Py TL', str(initial), 'Ren’Py TL (*.rpy)')
        if self._shut_down:
            return
        # Restore the export panel after the native chooser's window handoff,
        # including cancellation, without reopening a closed main window.
        self.show()
        self.raise_()
        self.activateWindow()
        if selected:
            self.prepare_target(Path(selected))

    def prepare_target(self, target):
        if self.operation_running or self._shut_down:
            return
        self.controller.cancel_tl_export_preview(self.view)
        self.view = self.result = None
        self._target = target
        self.target_label.setText(f'导出目标：{target}')
        self.summary_label.setText('正在计算修改段数、空译文与未确认数量…')
        self.diagnostics.clear()
        try:
            job = self.controller.begin_tl_export_preview(target)
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(f'无法准备导出：{error}')
            self._update_access()
            return
        self.status_label.setText('正在验证并准备预览…')
        self._start(job)

    def confirm_export(self):
        if self.operation_running or self.view is None:
            return
        try:
            job = self.controller.begin_tl_export_publish(self.view, self._target)
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(f'预览已过期，请重新预览。{error}')
            self.confirm_button.setEnabled(False)
            return
        self.status_label.setText('正在导出…已发出的发布按实际结果报告。')
        self._start(job)

    def _start(self, job):
        self._runner = QtProjectFileJob(job, self._finish, parent=self)
        self._update_access()
        self.activity_changed.emit()
        self._runner.start()

    def _finish(self, job):
        self._runner = None
        try:
            outcome = self.controller.finish_tl_export_job(job)
            if job.kind == 'prepare_export':
                self.view = outcome.result
                if not outcome.accepted:
                    if self.view is not None:
                        self.view = replace(self.view, status='cancelled' if outcome.cancelled else 'stale')
                    self.status_label.setText('准备已取消。' if outcome.cancelled else '预览已过期，请重新预览。')
                else:
                    self._show_preview()
            else:
                self.result = outcome.result
                self.status_label.setText(self.result_message(self.result))
                self._show_diagnostics(self.result.diagnostics)
                self.result_ready.emit(self.result)
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(f'导出操作失败：{error}')
        finally:
            self._update_access()
            self.activity_changed.emit()

    def _show_preview(self):
        view = self.view
        count = '无法确定' if view.modified_count is None else str(view.modified_count)
        self.summary_label.setText(
            f'修改段数：{count} / {view.segment_count}；空译文：{view.empty_count}；'
            f'未确认：{view.unconfirmed_count}。全部当前译文将导出，空译文不补源文。')
        self.status_label.setText({
            'ready': '预览就绪。请确认目标和诊断；导出不会保存项目包。',
            'blocked': '导出被阻断，请修复以下诊断后重新预览。',
            'stale': '预览已过期，请重新预览。',
            'cancelled': '准备已取消。',
        }.get(view.status, '无法准备导出。'))
        self._show_diagnostics(view.diagnostics)

    def _show_diagnostics(self, diagnostics):
        lines = []
        for item in diagnostics:
            location = item.source_ref
            if item.line_number is not None:
                location += f' · 行 {item.line_number}'
            number = self.controller.tl_export_diagnostic_segment_number(self.result or self.view, item)
            if number is not None:
                location += f' · 第 {number} 段'
            lines.append(f'{location}\n{item.code} · {item.safe_summary}')
        self.diagnostics.setPlainText('\n\n'.join(lines) or '无阻断诊断。')

    @staticmethod
    def result_message(result):
        messages = {
            'published': '导出成功',
            'failed': '导出失败；请检查诊断，重新预览后再决定是否重试',
            'uncertain': '导出结果不确定；需要检查目标文件，未确认成功',
            'cancelled': '导出已取消',
            'stale': '预览已过期；未发布，请重新预览',
            'blocked': '导出被阻断',
        }
        return f'{messages.get(result.outcome, "未取得导出成功确认")}：{result.target_path}。导出不会替代项目包保存。'

    def _locate_diagnostic(self):
        projection = self.result or self.view
        if projection is None:
            return
        for item in projection.diagnostics:
            if self.controller.tl_export_diagnostic_segment_number(projection, item) is not None:
                try:
                    self.controller.locate_tl_export_diagnostic(projection, item)
                    self.segment_located.emit()
                except EditorControllerError:
                    self.status_label.setText('诊断已过期，请重新预览。')
                return

    def _watch_context(self):
        # Pure context/identity check. Full settings, package and target I/O
        # happens once again on the confirmation worker, never on this timer.
        if self.operation_running or self.result is not None or self.view is None or self.view.status != 'ready':
            return
        if not self.controller.tl_export_preview_current(self.view, self._target):
            old = self.view
            self.view = replace(old, status='stale')
            self.controller.cancel_tl_export_preview(old)
            self.status_label.setText('预览已过期，请重新预览。')
            self._update_access()

    def _update_access(self):
        running = self.operation_running
        self.target_button.setEnabled(not running and not self._shut_down)
        self.confirm_button.setEnabled(
            not running and not self._shut_down and self.result is None
            and self.view is not None and self.view.status == 'ready'
            and self.controller.tl_export_preview_current(self.view, self._target))
        self.cancel_button.setEnabled(running)
        projection = self.result or self.view
        self.locate_button.setEnabled(
            not running and projection is not None and any(
                self.controller.tl_export_diagnostic_segment_number(projection, item) is not None
                for item in projection.diagnostics))

    def cancel_operation(self):
        if self._runner is not None:
            self._runner.cancel()
            self.status_label.setText('正在取消；已发出的发布将按实际结果报告。')
        else:
            self.controller.cancel_tl_export_preview(self.view)
            self._update_access()

    def reject(self):
        """Escape and explicit rejection share the nonblocking close path."""
        self.cancel_operation()
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()

    def shutdown(self):
        """Window teardown never waits for or closes an active publisher."""
        self._shut_down = True
        self._watch.stop()
        if self._runner is not None:
            self._runner.close()
            self._runner = None
        else:
            self.controller.cancel_tl_export_preview(self.view)
        self.hide()
