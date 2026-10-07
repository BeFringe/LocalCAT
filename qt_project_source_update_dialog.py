"""Source update review UI; all identity and apply authority stays in Controller."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from editor_controller import EditorControllerError
from qt_project_file_job import QtProjectFileJob


_CATEGORY_LABELS = {
    'unchanged': '未变化', 'source_changed': '源文变更', 'new': '新增',
    'removed': '已移除', 'ambiguous': '关联冲突', 'unresolved': '无法关联',
}


class QtProjectSourceUpdateDialog(QDialog):
    activity_changed = Signal()
    result_ready = Signal(object)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.view = None
        self.result_receipt = None
        self._root = controller.source_update_root
        self._runner = None
        self._shut_down = False
        self._documents = self._document_context()
        self.setWindowTitle('更新 TL 源模板')
        self.resize(840, 620)
        layout = QVBoxLayout(self)
        self.root_label = self._label(f'更新根目录：{self._root}' if self._root else '请选择更新后的 TL 根目录。')
        layout.addWidget(self.root_label)
        self.root_button = QPushButton('选择更新根目录…')
        self.root_button.clicked.connect(self.select_root)
        layout.addWidget(self.root_button)
        layout.addWidget(self._label('按当前章节顺序读取以下路径。文件改名时，请在右列明确填写新相对路径；不会自动猜测关联。'))
        self.paths = QTableWidget(len(self._documents), 2)
        self.paths.setHorizontalHeaderLabels(('当前相对路径', '更新后的相对路径（可编辑）'))
        self.paths.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, (_, _, ref) in enumerate(self._documents):
            self.paths.setItem(row, 0, self._readonly_item(ref))
            self.paths.setItem(row, 1, QTableWidgetItem(ref))
        self.paths.itemChanged.connect(self._input_changed)
        layout.addWidget(self.paths)
        self.summary_label = self._label('预览只显示变化类型、文件路径及段落位置。')
        layout.addWidget(self.summary_label)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(('类型', '相对路径', '段落', '明确处置'))
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.results)
        self.status_label = self._label('更新只改变当前内存中的项目；应用后请保存项目包。')
        layout.addWidget(self.status_label)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton('预览源更新')
        self.apply_button = QPushButton('应用源更新')
        self.cancel_button = QPushButton('取消')
        for button in (self.preview_button, self.apply_button, self.cancel_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        layout.addLayout(buttons)
        self.preview_button.clicked.connect(self.prepare_preview)
        self.apply_button.clicked.connect(self.apply_update)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self._watch = QTimer(self)
        self._watch.setInterval(100)
        self._watch.timeout.connect(self._watch_context)
        self._watch.start()
        self._update_access()

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _readonly_item(text):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    @property
    def operation_running(self):
        return self._runner is not None

    def _document_context(self):
        return tuple((item.identity.project.session_id, item.identity.document_id, item.source_ref)
                     for item in self.controller.source_update_documents)

    def _context_current(self):
        return self._documents == self._document_context()

    def select_root(self):
        if self.operation_running or self._shut_down:
            return
        selected = QFileDialog.getExistingDirectory(self, '选择更新后的 TL 根目录', str(self._root or ''))
        if self._shut_down:
            return
        self.show()
        self.raise_()
        self.activateWindow()
        if selected:
            self._root = Path(selected)
            self.root_label.setText(f'更新根目录：{selected}')
            self._input_changed()

    def _input_changed(self):
        self._discard_preview()
        self.result_receipt = None
        self.results.setRowCount(0)
        self.summary_label.setText('输入已更新，请重新预览。')
        self._update_access()

    def _discard_preview(self):
        if self.view is not None:
            self.controller.cancel_source_update_preview(self.view)
            self.view = None

    def prepare_preview(self):
        if self.operation_running or self._shut_down or self._root is None or not self._context_current():
            return
        self._input_changed()
        refs = tuple(self.paths.item(row, 1).text() for row in range(self.paths.rowCount()))
        try:
            job = self.controller.begin_source_update_preview(self._root, refs)
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(self._failure_message(error))
            return
        self.status_label.setText('正在读取并验证所选模板，可取消…')
        self._start(job)

    def apply_update(self):
        if not self.apply_button.isEnabled() or self.view is None:
            return
        dispositions = tuple(self.results.cellWidget(row, 3).currentData()
                             for row, item in enumerate(self.view.items) if item.required)
        try:
            job = self.controller.begin_source_update_apply(self.view, dispositions)
        except (EditorControllerError, OSError, ValueError) as error:
            self._discard_preview()
            self.status_label.setText(f'无法应用，请重新预览：{error}')
            self._update_access()
            return
        self.status_label.setText('正在复核源文件和当前项目；复核完成后应用…')
        self._start(job)

    def _start(self, job):
        self._runner = QtProjectFileJob(job, self._finish, parent=self)
        self._update_access()
        self.activity_changed.emit()
        self._runner.start()

    def _finish(self, job):
        self._runner = None
        try:
            outcome = self.controller.finish_source_update_job(job)
            if not outcome.accepted:
                self._discard_preview()
                self.status_label.setText('源更新已取消，当前项目未改变。' if outcome.cancelled
                                          else '预览已过期，当前项目未改变；请重新预览。')
            elif outcome.kind == 'preview_source_update':
                self.view = outcome.result
                self._show_preview()
            else:
                self.result_receipt = outcome.result
                self.view = None
                self.status_label.setText('源更新已应用到当前项目。请保存项目包以保留结果。')
                self.result_ready.emit(self.result_receipt)
        except (EditorControllerError, OSError, ValueError) as error:
            self._discard_preview()
            self.status_label.setText(self._failure_message(error))
        finally:
            self._update_access()
            self.activity_changed.emit()

    @staticmethod
    def _failure_message(error):
        if str(error) == 'PROJECT.RECONCILE.ROOT_REBIND_REQUIRED':
            return ('当前会话已绑定源根，请在原根更新；若需换根，请先保存项目包并重新打开，'
                    '再选择新根。当前项目未改变。')
        return f'源更新失败，当前项目未改变：{error}'

    def _show_preview(self):
        counts = {category: 0 for category in _CATEGORY_LABELS}
        self.results.setRowCount(len(self.view.items))
        for row, item in enumerate(self.view.items):
            counts[item.category] += 1
            for column, text in enumerate((_CATEGORY_LABELS[item.category], item.source_ref,
                    f'第 {item.segment_number} 段' if item.segment_number is not None else '—')):
                self.results.setItem(row, column, self._readonly_item(text))
            if item.required:
                choice = QComboBox()
                choice.addItem('请选择处置…', None)
                choice.addItem('保留为脱离源的段落', 'keep_detached')
                if item.category == 'removed':
                    choice.addItem('移除段落', 'remove')
                choice.currentIndexChanged.connect(self._update_access)
                self.results.setCellWidget(row, 3, choice)
            else:
                self.results.setItem(row, 3, self._readonly_item('无需处置'))
        self.summary_label.setText('；'.join(f'{_CATEGORY_LABELS[key]}：{count}' for key, count in counts.items()))
        self.status_label.setText('请核对变化并逐项处置；源文变更保留已有译文并撤销确认。应用后仍需保存项目包。')

    def _watch_context(self):
        if self.operation_running or self._shut_down or self.result_receipt is not None:
            return
        if self.view is not None and (not self._context_current()
                or not self.controller.source_update_preview_current(self.view)):
            self._discard_preview()
            self.status_label.setText('预览已过期，请重新预览；如已切换项目，请关闭并重新打开源更新。')
        self._update_access()

    def _update_access(self):
        idle = not self.operation_running and not self._shut_down
        current = self._context_current()
        self.root_button.setEnabled(idle and current)
        self.paths.setEnabled(idle and current)
        self.preview_button.setEnabled(idle and current and self._root is not None)
        self.results.setEnabled(idle)
        ready = idle and current and self.view is not None
        if ready:
            ready = self.controller.source_update_preview_current(self.view) and all(
                self.results.cellWidget(row, 3).currentData() is not None
                for row, item in enumerate(self.view.items) if item.required)
        self.apply_button.setEnabled(bool(ready))

    def cancel_operation(self):
        if self._runner is not None:
            self._runner.cancel()
            self.status_label.setText('正在取消源更新…')
        else:
            self.reject()

    def reject(self):
        self.shutdown()
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()

    def shutdown(self):
        self._shut_down = True
        self._watch.stop()
        if self._runner is not None:
            self._runner.close()
            self._runner = None
        self._discard_preview()
        self.hide()
