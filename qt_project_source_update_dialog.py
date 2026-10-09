"""Source update review UI; all identity and apply authority stays in Controller."""
from __future__ import annotations

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
)

from editor_controller import EditorControllerError
from editor_source_update import source_update_word_diff
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
        self._block_index = 0
        self._choices = {}
        self.setWindowTitle('更新 TL 源模板')
        self.resize(980, 740)
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
        self.paths.setMaximumHeight(self.paths.horizontalHeader().height()
                                    + min(len(self._documents), 3) * self.paths.verticalHeader().defaultSectionSize() + 8)
        layout.addWidget(self.paths)
        statistics = QHBoxLayout()
        self.summary_label = self._label('预览将显示变化段落的旧文与新文。')
        statistics.addWidget(self.summary_label, 1)
        self.previous_button = QPushButton('上一差异')
        self.next_button = QPushButton('下一差异')
        self.position_label = self._label('')
        self.position_label.setWordWrap(False)
        for name, widget in (('previousSourceDifference', self.previous_button),
                             ('sourceDifferencePosition', self.position_label),
                             ('nextSourceDifference', self.next_button)):
            widget.setObjectName(name)
            statistics.addWidget(widget)
        self.previous_button.clicked.connect(lambda: self._navigate(-1))
        self.next_button.clicked.connect(lambda: self._navigate(1))
        self.previous_button.setShortcut('Alt+Up')
        self.next_button.setShortcut('Alt+Down')
        self.previous_button.setToolTip('上一差异（Alt+↑）')
        self.next_button.setToolTip('下一差异（Alt+↓）')
        self.previous_button.setAutoDefault(False)
        self.next_button.setAutoDefault(False)
        layout.addLayout(statistics)
        self.diff_view = QTextBrowser()
        self.diff_view.setObjectName('sourceUpdateDiff')
        self.diff_view.setAccessibleName('源更新旧文与新文对照')
        self.diff_view.setOpenLinks(False)
        self.diff_view.setOpenExternalLinks(False)
        self.diff_view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                               | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        layout.addWidget(self.diff_view, 1)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(('当前差异类型', '相对路径', '段落', '明确处置'))
        self.results.setObjectName('sourceUpdateDispositions')
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.results.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.results.setMaximumHeight(150)
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
        self._clear_diff()
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
        self.summary_label.setText('输入已更新，请重新预览。')
        self._update_access()

    def _discard_preview(self):
        if self.view is not None:
            self.controller.cancel_source_update_preview(self.view)
            self.view = None
        self._clear_diff()

    def _clear_diff(self):
        self._choices.clear()
        self._block_index = 0
        self.results.setRowCount(0)
        self.results.hide()
        self.diff_view.clear()
        self.position_label.clear()
        for widget in (self.previous_button, self.position_label, self.next_button):
            widget.hide()

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
        dispositions = tuple(self._choices[index]
                             for index, item in enumerate(self.view.items) if item.required)
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
                self._clear_diff()
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
        for item in self.view.items:
            counts[item.category] += 1
        self.summary_label.setText('；'.join(f'{_CATEGORY_LABELS[key]}：{count}' for key, count in counts.items()))
        self._choices = {index: None for index, item in enumerate(self.view.items) if item.required}
        self._block_index = 0
        self._show_block()
        self.status_label.setText('请核对变化并逐项处置；源文变更保留已有译文并撤销确认。应用后仍需保存项目包。')

    def _navigate(self, offset):
        if self.view is None or self.operation_running or self._shut_down:
            return
        if not self._context_current() or not self.controller.source_update_preview_current(self.view):
            self._watch_context()
            return
        position = self._block_index + offset
        if 0 <= position < len(self.view.blocks):
            self._block_index = position
            self._show_block()
            self._update_access()

    def _show_block(self):
        self.results.setRowCount(0)
        if not self.view.blocks:
            self.diff_view.setPlainText('模板一致，没有源文差异。' if self.view.templates_identical else
                '源文与段落标识一致；模板其他内容有变化，请核对路径后应用。')
            self.results.hide()
            for widget in (self.previous_button, self.position_label, self.next_button):
                widget.hide()
            return
        self.results.show()
        for widget in (self.previous_button, self.position_label, self.next_button):
            widget.show()
        block = self.view.blocks[self._block_index]
        self.position_label.setText(f'{self._block_index + 1} / {len(self.view.blocks)}')
        self.results.setRowCount(len(block.item_indices))
        self.results.setMaximumHeight(self.results.horizontalHeader().height()
                                      + min(len(block.item_indices), 3) * self.results.verticalHeader().defaultSectionSize() + 8)
        for row, index in enumerate(block.item_indices):
            item = self.view.items[index]
            positions = []
            if item.old:
                positions.append(f'旧：第 {item.old.segment_number} 段')
            if item.new:
                positions.append(f'新：第 {item.new.segment_number} 段')
            for column, text in enumerate((_CATEGORY_LABELS[item.category], item.source_ref,
                                          ' / '.join(positions) or '—')):
                self.results.setItem(row, column, self._readonly_item(text))
            if item.required:
                choice = QComboBox()
                choice.addItem('请选择处置…', None)
                choice.addItem('保留为脱离源的段落', 'keep_detached')
                if item.category == 'removed':
                    choice.addItem('移除段落', 'remove')
                choice.setCurrentIndex(choice.findData(self._choices[index]))
                choice.currentIndexChanged.connect(
                    lambda _, item_index=index, widget=choice: self._choose(item_index, widget.currentData()))
                self.results.setCellWidget(row, 3, choice)
            else:
                self.results.setItem(row, 3, self._readonly_item('无需处置'))
        self.diff_view.setHtml(self._block_html(block))
        self.diff_view.verticalScrollBar().setValue(0)

    def _choose(self, index, value):
        self._choices[index] = value
        self._update_access()

    def _block_html(self, block):
        categories = {self.view.items[index].category for index in block.item_indices}
        chunks = []
        if 'removed' in categories and 'new' in categories:
            chunks.append('<p>同一区间的旧文／新文仅作对照，仍计为新增／已移除；译文不会自动迁移。</p>')
        elif categories & {'ambiguous', 'unresolved'}:
            chunks.append('<p>这些段落无法唯一关联，请明确处置；不会根据位置或相似文本猜测。</p>')
        word_diff = None
        if len(block.old) == len(block.new) == 1:
            word_diff = source_update_word_diff(block.old[0].source, block.new[0].source)
            if not word_diff.highlighted:
                chunks.append('<p>段落较长，未做词级高亮；以下保留完整旧文与新文。</p>')
        for side, texts, sign, label, background, highlight in (
                ('old', block.old, '−', '旧文', '#fff0f0', '#f4a8ad'),
                ('new', block.new, '+', '新文', '#edf8ee', '#a5d9b0')):
            if not texts:
                chunks.append(f'<p>{sign} {label}：无</p>')
            for text in texts:
                parts = getattr(word_diff, side) if word_diff else ((text.source, False),)
                body = ''.join(f'<span style="background-color:{highlight};">{escape(value)}</span>'
                               if changed else escape(value) for value, changed in parts)
                chunks.append(f'<p><b>{sign} {label}</b> · {escape(text.source_ref)} · 第 {text.segment_number} 段</p>'
                    f'<table width="100%" cellpadding="10" cellspacing="0" bgcolor="{background}"><tr><td>'
                    f'<p style="white-space:pre-wrap; color:#202020;">{body}</p></td></tr></table>')
                if side == 'old' and text.target:
                    chunks.append(f'<p style="white-space:pre-wrap;">旧译文（供核对）：{escape(text.target)}</p>')
        return ''.join(chunks)

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
        self.diff_view.setEnabled(idle)
        has_blocks = self.view is not None and bool(self.view.blocks)
        self.previous_button.setEnabled(idle and current and has_blocks and self._block_index > 0)
        self.next_button.setEnabled(idle and current and has_blocks
                                    and self._block_index + 1 < len(self.view.blocks))
        ready = idle and current and self.view is not None
        if ready:
            ready = self.controller.source_update_preview_current(self.view) and all(
                self._choices.get(index) is not None
                for index, item in enumerate(self.view.items) if item.required)
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
