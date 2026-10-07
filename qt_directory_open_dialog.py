"""Display-only directory selection and the unified local-open chooser."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout)


class QtLocalProjectOpenDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('localProjectOpenDialog')
        self.setWindowTitle('打开本地项目')
        self.selection_kind = 'files'
        layout = QVBoxLayout(self)
        for kind, text in (('files', '选择文件（Shift 可多选）'),
                           ('folder', '选择文件夹（递归预览并勾选）')):
            button = QPushButton(text)
            button.setObjectName('openLocal' + kind.title())
            button.clicked.connect(lambda checked=False, kind=kind: self._choose(kind))
            layout.addWidget(button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, kind):
        self.selection_kind = kind
        self.accept()


class QtDirectoryOpenDialog(QDialog):
    """Only issued entry IDs leave this review; no filesystem access occurs."""
    _REASONS = {'directory': '目录', 'link': '链接不可导入', 'reparse': '重解析点不可导入',
        'not_regular': '不是普通文件', 'invalid_reference': '路径名称不可移植',
        'unsupported_format': '当前配置不支持此格式'}

    def __init__(self, review, *, current=lambda: True, parent=None):
        super().__init__(parent)
        self.review, self._current = review, current
        self._order_changed = False
        self.setObjectName('directoryOpenDialog')
        self.setWindowTitle('选择目录中的项目文档')
        self.resize(800, 660)
        layout = QVBoxLayout(self)
        self.hint = QLabel('这里只预览目录。勾选文件并确认顺序后，将验证所选内容并建立项目包。原文件保持只读。')
        self.hint.setObjectName('directoryPreviewHint')
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setObjectName('directoryCandidateTree')
        self.tree.setHeaderLabels(('相对路径', '状态'))
        self.entry_items = {}
        for entry in review.preview.entries:
            item = QTreeWidgetItem((entry.display_name,
                '可选择 · ' + str(entry.byte_count) + ' 字节' if entry.selectable
                else self._REASONS.get(entry.unavailable_reason.value, '不可选择')))
            item.setData(0, Qt.ItemDataRole.UserRole, entry.entry_id)
            item.setToolTip(0, entry.source_ref or entry.display_name)
            parent_item = self.entry_items.get(entry.parent_entry_id)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            if entry.selectable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            self.entry_items[entry.entry_id] = item
        self.tree.expandToDepth(2)
        self.tree.setColumnWidth(0, 480)
        layout.addWidget(self.tree, 2)
        layout.addWidget(QLabel('所选章节顺序（完整相对路径）'))
        self.selected_files = QListWidget()
        self.selected_files.setObjectName('directorySelectedFiles')
        layout.addWidget(self.selected_files, 1)
        order = QHBoxLayout()
        for delta, name, text in ((-1, 'directoryMoveUp', '上移'), (1, 'directoryMoveDown', '下移')):
            button = QPushButton(text)
            button.setObjectName(name)
            button.clicked.connect(lambda checked=False, delta=delta: self.move_selected(delta))
            order.addWidget(button)
        order.addStretch()
        layout.addLayout(order)
        metadata = QHBoxLayout()
        self.project_name_input = QLineEdit(review.default_name)
        self.source_locale_input, self.target_locale_input = QLineEdit(), QLineEdit()
        for label, field, name, placeholder in (
                ('项目名', self.project_name_input, 'directoryProjectName', ''),
                ('源语言', self.source_locale_input, 'directorySourceLocale', '默认 en'),
                ('目标语言', self.target_locale_input, 'directoryTargetLocale', '默认 zh-CN')):
            metadata.addWidget(QLabel(label))
            field.setObjectName(name)
            field.setPlaceholderText(placeholder)
            metadata.addWidget(field)
        layout.addLayout(metadata)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('验证并建立项目包')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.tree.itemChanged.connect(self._selection_changed)
        self.project_name_input.textChanged.connect(self._update_ready)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._update_ready)
        self._timer.start()
        self._update_ready()

    @property
    def ordered_entry_ids(self):
        return tuple(self.selected_files.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.selected_files.count()))

    @property
    def source_locale(self):
        return self.source_locale_input.text().strip() or 'en'

    @property
    def target_locale(self):
        return self.target_locale_input.text().strip() or 'zh-CN'

    def _selection_changed(self, item, column):
        checked = {entry.entry_id for entry in self.review.preview.entries if entry.selectable
            and self.entry_items[entry.entry_id].checkState(0) == Qt.CheckState.Checked}
        # Keep deliberate reorder, adding newly checked entries in preview order.
        ordered = ([entry_id for entry_id in self.ordered_entry_ids if entry_id in checked]
                   if self._order_changed else [])
        retained = set(ordered)
        ordered.extend(entry.entry_id for entry in self.review.preview.entries
            if entry.entry_id in checked and entry.entry_id not in retained)
        entries = {entry.entry_id: entry for entry in self.review.preview.entries}
        self.selected_files.clear()
        for entry_id in ordered:
            selected = QListWidgetItem(entries[entry_id].source_ref)
            selected.setData(Qt.ItemDataRole.UserRole, entry_id)
            self.selected_files.addItem(selected)
        self._update_ready()

    def move_selected(self, delta):
        row = self.selected_files.currentRow()
        target = row + delta
        if delta not in (-1, 1) or row < 0 or target < 0 or target >= self.selected_files.count():
            return
        self._order_changed = True
        item = self.selected_files.takeItem(row)
        self.selected_files.insertItem(target, item)
        self.selected_files.setCurrentRow(target)

    def _update_ready(self):
        current = self._current()
        complete = self.review.preview.complete
        ready = current and complete and self.selected_files.count() > 0 and bool(self.project_name_input.text().strip())
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ready)
        self.tree.setEnabled(current and complete)
        if not current:
            self.hint.setText('此预览已过期。请取消并重新选择文件夹。')
        elif not complete:
            code = self.review.preview.rejection.code.value if self.review.preview.rejection else ''
            self.hint.setText('目录预览不完整，不能建立项目包。请选择较小的目录或检查访问权限后重试。 ' + code)
        return ready

    def _accept_if_valid(self):
        if self._update_ready():
            self.accept()
