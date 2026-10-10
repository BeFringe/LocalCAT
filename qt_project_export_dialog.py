"""Single and selected-file export UI over Controller display projections."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
    QTreeWidget, QTreeWidgetItem, QHeaderView,
)

from editor_controller import EditorControllerError
from qt_project_file_job import QtProjectFileJob
from project_export_contracts import ProjectExportBatchView, ProjectExportBatchResult, ProjectExportDirectoryResult


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
        self._dismissed = False
        self._document_items = {}
        self._selection_session = None
        self._batch_mode = False
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
        self.files_table = QTreeWidget()
        self.files_table.setHeaderLabels(['相对路径（勾选导出）', '操作／结果', '修改', '空译文', '未确认'])
        self.files_table.setRootIsDecorated(False)
        self.files_table.setAlternatingRowColors(True)
        self.files_table.setAccessibleName('TL 章节选择与逐文件导出结果')
        self.files_table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            self.files_table.header().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.files_table.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.files_table, 1)
        self.directory_label = QLabel()
        self.directory_label.setTextFormat(Qt.TextFormat.PlainText)
        self.directory_label.setWordWrap(True)
        layout.addWidget(self.directory_label)
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
        self._refresh_documents()
        self._update_access()

    @property
    def operation_running(self):
        return self._runner is not None

    def _refresh_documents(self):
        self._batch_mode = self.controller.tl_export_uses_directory
        self.files_table.setVisible(self._batch_mode)
        self.diagnostics.setMaximumHeight(110 if self._batch_mode else 16777215)
        if not self._batch_mode:
            return
        documents = self.controller.tl_export_documents
        session = self.controller.workspace_view.project.session_id
        previous = {key: item.checkState(0) for key, (item, _) in self._document_items.items()}
        preserve = session == self._selection_session
        self._selection_session = session
        self.files_table.blockSignals(True)
        self.files_table.clear()
        self._document_items = {}
        for document in documents:
            identity = document.identity
            item = QTreeWidgetItem([document.source_ref, '待预览', '—', '—', '—'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, previous.get(identity.document_id, Qt.CheckState.Checked)
                               if preserve else Qt.CheckState.Checked)
            item.setToolTip(0, document.source_ref)
            self.files_table.addTopLevelItem(item)
            self._document_items[identity.document_id] = (item, identity)
        self.files_table.blockSignals(False)
        self.resize(max(self.width(), 900), max(self.height(), 570))
        if (type(self.view) is ProjectExportBatchView
                and self.controller.tl_export_preview_current(self.view, self._target)):
            self._show_files(self.view)
        elif type(self.result) is ProjectExportBatchResult and self.result.session_id == session:
            self._show_files(self.result)

    def _selection_changed(self, _item, column):
        if column != 0 or self.operation_running:
            return
        self.controller.cancel_tl_export_preview(self.view)
        self.view = self.result = None
        self.status_label.setText('章节选择已更改，请重新预览。')
        self._update_access()

    def _show_files(self, projection):
        if not self._batch_mode:
            return
        facts = {item.document_id: item for item in projection.files}
        outcomes = {'published': '已导出', 'failed': '失败', 'uncertain': '不确定／需检查',
                    'not_attempted': '未执行', 'cancelled': '已取消', 'stale': '已过期', 'blocked': '已阻断'}
        self.files_table.blockSignals(True)
        for document_id, (item, _) in self._document_items.items():
            fact = facts.get(document_id)
            if fact is None:
                item.setText(1, '未选择')
                for column in range(2, 5):
                    item.setText(column, '—')
                continue
            if type(projection) is ProjectExportBatchResult:
                item.setText(1, outcomes.get(fact.outcome, fact.outcome))
            else:
                action = '覆盖' if fact.target_action == 'overwrite' else '新建'
                item.setText(1, action if fact.status == 'ready' else outcomes.get(fact.status, '未就绪'))
                for column, value in enumerate((fact.modified_count, fact.empty_count, fact.unconfirmed_count), 2):
                    item.setText(column, '—' if value is None else str(value))
            item.setToolTip(1, fact.target_path)
        self.files_table.blockSignals(False)

    def select_target(self, _checked=False, *, initial=None):
        if self.operation_running or self._shut_down:
            return
        self._dismissed = False
        self._refresh_documents()
        initial = initial or self._target or self.controller.tl_export_default_path
        if initial is None:
            self.status_label.setText('当前项目不能导出 TL。')
            return
        if self._batch_mode:
            selected = QFileDialog.getExistingDirectory(self, '选择 TL 导出根目录', str(initial))
        else:
            selected, _ = QFileDialog.getSaveFileName(
                self, '导出 Ren’Py TL', str(initial), 'Ren’Py TL (*.rpy)',
                options=QFileDialog.Option.DontConfirmOverwrite)
        if self._shut_down:
            return
        # Restore the export panel after the native chooser's window handoff,
        # including cancellation, without reopening a closed main window.
        self.show()
        self.raise_()
        self.activateWindow()
        if selected:
            self.prepare_target(Path(selected))

    def prepare_target(self, target, *, keep_directory_facts=False):
        if self.operation_running or self._shut_down:
            return
        self._refresh_documents()
        self.controller.cancel_tl_export_preview(self.view)
        self.view = self.result = None
        if not keep_directory_facts:
            self.directory_label.clear()
        self._target = target
        self.target_label.setText(f'导出目标：{target}')
        self.summary_label.setText('正在计算修改段数、空译文与未确认数量…')
        self.diagnostics.clear()
        try:
            selection = (tuple(identity for item, identity in self._document_items.values()
                               if item.checkState(0) == Qt.CheckState.Checked) if self._batch_mode else None)
            job = self.controller.begin_tl_export_preview(target, selection=selection)
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
            directories = type(self.view) is ProjectExportBatchView and bool(self.view.missing_directories)
            operation = (self.controller.begin_tl_export_directory_preparation if directories
                         else self.controller.begin_tl_export_publish)
            job = operation(self.view, self._target)
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(f'预览已过期，请重新预览。{error}')
            self.confirm_button.setEnabled(False)
            return
        self.status_label.setText('正在准备目录；完成后需要再次确认导出。' if directories
                                  else '正在导出…已发出的发布按实际结果报告。')
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
            elif job.kind == 'prepare_export_directories':
                self.result = outcome.result
                self.directory_label.setText(self.result_message(self.result))
                self._show_diagnostics(self.result.diagnostics)
                facts = [f'已创建：{path}' for path in self.result.created_directories]
                facts.extend(f'{"不确定" if item.outcome == "uncertain" else "失败"}：{item.relative_path} · {item.code}'
                             for item in self.result.directory_failures)
                self.directory_label.setToolTip('\n'.join(facts))
                if facts:
                    self.diagnostics.appendPlainText('\n目录操作记录：\n' + '\n'.join(facts))
                self.result_ready.emit(self.result)
                if (outcome.accepted and not outcome.cancelled and self.result.outcome == 'prepared'
                        and not self._dismissed and not self._shut_down):
                    self.prepare_target(self._target, keep_directory_facts=True)
                else:
                    self.status_label.setText('目录准备已结束；未发布 TL。重新导出需要新的预览。')
            else:
                self.result = outcome.result
                if type(self.result) is ProjectExportBatchResult:
                    self._show_files(self.result)
                self.status_label.setText(self.result_message(self.result))
                self._show_diagnostics(self.result.diagnostics)
                self.result_ready.emit(self.result)
                if (type(self.result) is not ProjectExportBatchResult
                        and self.result.outcome == 'published' and self.isVisible()):
                    self.accept()
        except (EditorControllerError, OSError, ValueError) as error:
            self.status_label.setText(f'导出操作失败：{error}')
        finally:
            self._update_access()
            self.activity_changed.emit()

    def _show_preview(self):
        view = self.view
        files = view.files if type(view) is ProjectExportBatchView else (view,)
        modified = None if any(item.modified_count is None for item in files) else sum(item.modified_count for item in files)
        count = '无法确定' if modified is None else str(modified)
        overwrite = sum(item.target_action == 'overwrite' for item in files)
        self.summary_label.setText(
            f'{len(files)} 个文件：新建 {len(files) - overwrite}，覆盖 {overwrite}。'
            f'修改段数：{count} / {sum(item.segment_count for item in files)}；'
            f'空译文：{sum(item.empty_count for item in files)}；未确认：{sum(item.unconfirmed_count for item in files)}。'
            '全部当前译文将导出，空译文不补源文。')
        if type(view) is ProjectExportBatchView:
            self._show_files(view)
            if view.missing_directories:
                self.directory_label.setText(
                    f'缺失 {len(view.missing_directories)} 个子目录。先准备目录，再重新预览并确认导出。')
                self.directory_label.setToolTip('\n'.join(view.missing_directories))
                self.confirm_button.setText('准备目录并重新预览')
            else:
                self.confirm_button.setText(f'确认导出 {len(files)} 个文件' + (f'（覆盖 {overwrite}）' if overwrite else ''))
        else:
            self.confirm_button.setText('确认覆盖并导出' if overwrite else '确认导出')
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
        if type(result) is ProjectExportDirectoryResult:
            created = len(result.created_directories)
            failures = len(result.directory_failures)
            state = {'prepared': '目录准备完成', 'cancelled': '目录准备已取消',
                     'failed': '目录准备失败', 'uncertain': '目录准备结果需检查'}.get(result.outcome, '目录准备未完成')
            return f'{state}：已创建 {created} 个目录，失败／需检查 {failures} 个。未发布 TL；新预览仍需确认。'
        if type(result) is ProjectExportBatchResult:
            counts = {name: sum(item.outcome == name for item in result.files)
                      for name in ('published', 'failed', 'uncertain', 'not_attempted')}
            return (f'已导出 {counts["published"]}，失败 {counts["failed"]}，不确定 {counts["uncertain"]}，'
                    f'未执行 {counts["not_attempted"]}：{result.target_path}。导出不会替代项目包保存。')
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
        self.files_table.setEnabled(not running and not self._shut_down)
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
        self._dismissed = True
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
