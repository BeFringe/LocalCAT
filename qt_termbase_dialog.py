"""Controller-only PySide6 termbase management dialog for LocalCAT."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from editor_contracts import (
    TermCommitOutcome,
    TermCommitState,
    TermDraft,
    TermMatchPolicy,
    TermRecord,
    TextMatcherDisplayState,
    TextMatcherState,
)
from editor_controller import EditorController, EditorControllerError
from qt_localized_message_box import ask_localized_question


_TERM_ERROR_MESSAGES = {
    "DUPLICATE_SOURCE": "源术语重复；请修改现有记录或更换源术语。",
    "CONFLICTING_SOURCE": "源术语与另一条记录冲突，请使用唯一的源术语。",
    "STALE_LOCATOR": "记录已变化，请重新打开术语管理后再试。",
    "EMPTY_SOURCE": "源术语不能为空。",
    "EMPTY_TARGET": "目标术语不能为空。",
    "EMPTY_ROW": "术语文件含有空记录，请先修复文件。",
    "INVALID_UTF8": "术语文件不是有效 UTF-8，请先修复文件。",
    "INVALID_COLUMN_COUNT": "术语文件行格式无效，请先修复文件。",
    "INVALID_BOOLEAN": "术语匹配选项无效，请先修复文件。",
    "UNKNOWN_MARKER": "术语文件版本标记无法识别。",
    "MALFORMED_CSV": "术语 CSV 格式无效，请先修复文件。",
    "TERM.RESOURCE_UNKNOWN": "术语资源不存在，请返回设置刷新资源。",
    "TERM.RESOURCE_KIND_INVALID": "当前资源不是术语表。",
    "TERM.RESOURCE_NOT_WRITABLE": (
        "当前术语表不可写；请在语言资源设置中开启 Active 和 Update。"
    ),
    "TERM.RUNTIME_UNAVAILABLE": "术语运行时尚不可用，请刷新资源后再试。",
    "TERM.RUNTIME_INVALID": "术语运行时状态无法安全验证。",
    "TERM.PREPARE_FAILED": "术语变更未能安全准备，原记录保持不变。",
    "TERM.CANDIDATE_BUILD_FAILED": "术语候选匹配器未能完整构建，原记录保持不变。",
    "TERM.MATCHER_GENERATION_CHANGED": "匹配能力已变化，请重新打开术语管理后再试。",
    "TERM.QUARANTINE_INVALID": "术语隔离状态无法安全验证，请先恢复资源。",
}
_TERM_COMMIT_ERROR_CODES = frozenset(
    {
        "COMMIT_VERIFICATION_FAILED",
        "DIRECTORY_FSYNC_FAILED",
        "RECOVERY_CHANGED",
        "RECOVERY_DELETE_FAILED",
        "RECOVERY_READ_FAILED",
        "RECOVERY_UNAVAILABLE",
        "REPLACE_FAILED",
        "ROLLBACK_FAILED",
        "ROLLBACK_VERIFICATION_FAILED",
        "SOURCE_CHANGED",
        "SOURCE_DIGEST_FAILED",
        "STAGED_CHANGED",
        "STAGED_READ_FAILED",
    }
)


def _controller_error_feedback(error: EditorControllerError) -> str:
    """Project one body-free Controller error into actionable UI text."""

    code = (
        error.args[0]
        if type(error) is EditorControllerError
        and len(error.args) == 1
        and type(error.args[0]) is str
        else None
    )
    if code is None:
        return "术语操作未能安全完成；原列表保持不变。"
    message = _TERM_ERROR_MESSAGES.get(code)
    if message is not None:
        return message
    return "术语操作未能安全完成；原列表保持不变。"


class QtTermbaseDialog(QDialog):
    """Manage one writable termbase exclusively through EditorController."""

    terms_committed = Signal()

    def __init__(
        self,
        controller: EditorController,
        resource_id: str,
        resource_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(controller, EditorController):
            raise TypeError("term dialog controller must be an EditorController")
        if type(resource_id) is not str or not resource_id.strip():
            raise ValueError("term dialog resource id must not be empty")
        if type(resource_name) is not str or not resource_name.strip():
            raise ValueError("term dialog resource name must not be empty")
        self.controller = controller
        self.resource_id = resource_id
        self.resource_name = resource_name
        self._rows: tuple[TermRecord, ...] = ()
        self._selected_record: TermRecord | None = None
        self._creating = False
        self._list_available = False
        self._matcher_display = self._read_matcher_display()

        self.setObjectName("termbaseDialog")
        self.setWindowTitle(f"LocalCAT · {resource_name} 术语管理")
        self.setAccessibleName(f"{resource_name} 术语管理")
        self.setModal(True)
        self.setMinimumSize(760, 520)
        self.resize(900, 620)
        self._build_ui()
        self._load_terms()

    def _read_matcher_display(self) -> TextMatcherDisplayState:
        display = self.controller.term_matcher_display()
        if type(display) is not TextMatcherDisplayState:
            raise TypeError(
                "term matcher display must be TextMatcherDisplayState"
            )
        display.__post_init__()
        return display

    @property
    def _configured_flags_editable(self) -> bool:
        return (
            self._matcher_display.state
            is TextMatcherState.TEXT_V1_VALIDATED
        )

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)

        title = QLabel("术语管理")
        title.setObjectName("termDialogTitle")
        title.setAccessibleName("术语管理")
        root.addWidget(title)
        resource = QLabel(f"资源：{self.resource_name}")
        resource.setObjectName("termResourceName")
        resource.setAccessibleName(f"当前术语资源 {self.resource_name}")
        root.addWidget(resource)

        self.capability_label = QLabel()
        self.capability_label.setObjectName("termMatcherCapability")
        self.capability_label.setWordWrap(True)
        self.capability_label.setAccessibleName("术语匹配能力状态")
        self.capability_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.capability_label)

        self.term_table = QTableWidget(0, 5)
        self.term_table.setObjectName("termTable")
        self.term_table.setAccessibleName(f"{self.resource_name} 术语列表")
        self.term_table.setToolTip("选择一条术语以编辑或删除")
        self.term_table.setHorizontalHeaderLabels(
            ["源术语", "目标术语", "Policy", "Match Case", "Whole Word"]
        )
        self.term_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.term_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.term_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.term_table.setTabKeyNavigation(False)
        self.term_table.setAlternatingRowColors(True)
        self.term_table.verticalHeader().setVisible(False)
        header = self.term_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.ResizeToContents,
            )
        self.term_table.itemSelectionChanged.connect(self._load_selected)
        root.addWidget(self.term_table, 1)

        editor = QFrame()
        editor.setObjectName("termEditorPanel")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(12, 10, 12, 10)
        editor_layout.setSpacing(8)
        form = QFormLayout()
        self.source_input = QLineEdit()
        self.source_input.setObjectName("termSource")
        self.source_input.setAccessibleName("源术语")
        self.source_input.setToolTip("输入术语的源文文本")
        self.target_input = QLineEdit()
        self.target_input.setObjectName("termTarget")
        self.target_input.setAccessibleName("目标术语")
        self.target_input.setToolTip("输入术语的目标文文本")
        form.addRow("源术语", self.source_input)
        form.addRow("目标术语", self.target_input)
        editor_layout.addLayout(form)

        self.policy_label = QLabel()
        self.policy_label.setObjectName("termPolicy")
        self.policy_label.setWordWrap(True)
        self.policy_label.setAccessibleName("术语记录策略")
        editor_layout.addWidget(self.policy_label)

        flags = QHBoxLayout()
        self.match_case_checkbox = QCheckBox("Match Case")
        self.match_case_checkbox.setObjectName("termMatchCase")
        self.match_case_checkbox.setAccessibleName("术语区分大小写")
        self.match_case_checkbox.setToolTip("匹配时区分大小写")
        self.whole_word_checkbox = QCheckBox("Whole Word")
        self.whole_word_checkbox.setObjectName("termWholeWord")
        self.whole_word_checkbox.setAccessibleName("术语全词匹配")
        self.whole_word_checkbox.setToolTip("仅匹配完整单词；纯 CJK 保持连续文本匹配")
        flags.addWidget(self.match_case_checkbox)
        flags.addWidget(self.whole_word_checkbox)
        flags.addStretch()
        editor_layout.addLayout(flags)

        actions = QHBoxLayout()
        self.create_button = QPushButton("新建术语")
        self.create_button.setObjectName("newTerm")
        self.create_button.setAccessibleName("新建术语")
        self.create_button.setToolTip("开始新建 configured 术语")
        self.create_button.clicked.connect(self._begin_create)
        self.save_button = QPushButton("保存术语")
        self.save_button.setObjectName("saveTerm")
        self.save_button.setAccessibleName("保存术语")
        self.save_button.setToolTip("保存当前新建或编辑的术语")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save_term)
        self.delete_button = QPushButton("删除术语")
        self.delete_button.setObjectName("deleteTerm")
        self.delete_button.setAccessibleName("删除术语")
        self.delete_button.setToolTip("确认后删除所选术语")
        self.delete_button.clicked.connect(self._delete_term)
        actions.addWidget(self.create_button)
        actions.addWidget(self.save_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        editor_layout.addLayout(actions)
        root.addWidget(editor)

        self.feedback_label = QLabel()
        self.feedback_label.setObjectName("termFeedback")
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setAccessibleName("术语操作结果")
        self.feedback_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.feedback_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("完成")
        self.close_button.setObjectName("closeTermDialog")
        self.close_button.setAccessibleName("关闭术语管理")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.setTabOrder(self.term_table, self.source_input)
        self.setTabOrder(self.source_input, self.target_input)
        self.setTabOrder(self.target_input, self.match_case_checkbox)
        self.setTabOrder(self.match_case_checkbox, self.whole_word_checkbox)
        self.setTabOrder(self.whole_word_checkbox, self.create_button)
        self.setTabOrder(self.create_button, self.save_button)
        self.setTabOrder(self.save_button, self.delete_button)
        self.setTabOrder(self.delete_button, self.close_button)
        self.setStyleSheet(_TERM_DIALOG_STYLE)

    def _load_terms(
        self,
        *,
        preferred_source: str | None = None,
        preferred_row: int | None = None,
    ) -> None:
        try:
            rows = self.controller.list_terms(self.resource_id)
        except EditorControllerError as error:
            self._list_available = False
            self._rows = ()
            self.term_table.setRowCount(0)
            self._selected_record = None
            self._creating = False
            self._show_feedback(_controller_error_feedback(error), failed=True)
            self._sync_editor_state()
            return
        if type(rows) is not tuple or any(
            type(record) is not TermRecord for record in rows
        ):
            raise TypeError("term list must contain exact TermRecord values")
        for record in rows:
            record.__post_init__()
        self._list_available = True
        self._rows = rows
        self.term_table.setRowCount(len(rows))
        for row_index, record in enumerate(rows):
            values = (
                record.source,
                record.target,
                "Legacy"
                if record.policy is TermMatchPolicy.LEGACY
                else "Configured",
                "—" if record.match_case is None else ("是" if record.match_case else "否"),
                "—" if record.whole_word is None else ("是" if record.whole_word else "否"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter
                    | (
                        Qt.AlignmentFlag.AlignLeft
                        if column < 2
                        else Qt.AlignmentFlag.AlignCenter
                    )
                )
                self.term_table.setItem(row_index, column, item)

        selection = -1
        if preferred_source is not None:
            selection = next(
                (
                    index
                    for index, record in enumerate(rows)
                    if record.source == preferred_source
                ),
                -1,
            )
        if selection < 0 and preferred_row is not None and rows:
            selection = min(max(preferred_row, 0), len(rows) - 1)
        if selection < 0 and rows:
            selection = 0
        if selection >= 0:
            self.term_table.setCurrentCell(selection, 0)
            self.term_table.selectRow(selection)
            self._load_selected()
        else:
            self._selected_record = None
            self._creating = True
            self.source_input.clear()
            self.target_input.clear()
            self.match_case_checkbox.setChecked(False)
            self.whole_word_checkbox.setChecked(True)
            self._sync_editor_state()

    def _load_selected(self) -> None:
        row = self.term_table.currentRow()
        if row < 0 or row >= len(self._rows):
            return
        record = self._rows[row]
        self._selected_record = record
        self._creating = False
        self.source_input.setText(record.source)
        self.target_input.setText(record.target)
        if record.policy is TermMatchPolicy.LEGACY:
            self.match_case_checkbox.setChecked(False)
            self.whole_word_checkbox.setChecked(False)
        else:
            if record.match_case is None or record.whole_word is None:
                raise AssertionError("configured term record lost its flags")
            self.match_case_checkbox.setChecked(record.match_case)
            self.whole_word_checkbox.setChecked(record.whole_word)
        self._sync_editor_state()

    def _begin_create(self) -> None:
        self.term_table.clearSelection()
        self.term_table.setCurrentCell(-1, -1)
        self._selected_record = None
        self._creating = True
        self.source_input.clear()
        self.target_input.clear()
        self.match_case_checkbox.setChecked(False)
        self.whole_word_checkbox.setChecked(True)
        self._sync_editor_state()
        self.source_input.setFocus()

    def _sync_editor_state(self) -> None:
        record = self._selected_record
        is_legacy = (
            record is not None
            and record.policy is TermMatchPolicy.LEGACY
        )
        show_flags = not is_legacy
        self.match_case_checkbox.setVisible(show_flags)
        self.whole_word_checkbox.setVisible(show_flags)
        self.match_case_checkbox.setEnabled(
            show_flags
            and self._configured_flags_editable
        )
        self.whole_word_checkbox.setEnabled(
            show_flags
            and self._configured_flags_editable
        )
        self.delete_button.setEnabled(record is not None and not self._creating)
        self.create_button.setEnabled(self._list_available)
        self.save_button.setEnabled(self._list_available)

        if is_legacy:
            self.policy_label.setText(
                "Legacy 两列记录：无 Match Case / Whole Word flags；"
                "保存时继续保持两列，不会静默迁移。"
            )
            self.capability_label.setText(
                "Legacy 使用既有区分大小写的连续子串匹配。"
            )
            return

        self.policy_label.setText(
            "Configured 记录：新建默认 Match Case=否、Whole Word=是。"
        )
        if self._configured_flags_editable:
            self.capability_label.setText(
                "TEXT_V1 匹配已验收；保存的 Match Case / Whole Word 选项会参与匹配。"
            )
        else:
            self.capability_label.setText(
                "Match Case / Whole Word 已保存但尚不参与匹配；"
                "TEXT_V1 能力验收前按 legacy preset 使用。"
            )

    def _draft_from_editor(self) -> TermDraft:
        try:
            return TermDraft(
                source=self.source_input.text(),
                target=self.target_input.text(),
                match_case=self.match_case_checkbox.isChecked(),
                whole_word=self.whole_word_checkbox.isChecked(),
            )
        except ValueError as error:
            raise EditorControllerError(
                "EMPTY_SOURCE"
                if not self.source_input.text().strip()
                else "EMPTY_TARGET"
            ) from error

    def _save_term(self) -> None:
        try:
            draft = self._draft_from_editor()
            record = self._selected_record
            if self._creating or record is None:
                outcome = self.controller.create_term(self.resource_id, draft)
                preferred_row = self.term_table.rowCount()
            else:
                outcome = self.controller.update_term(
                    self.resource_id,
                    record.locator,
                    draft,
                )
                preferred_row = self.term_table.currentRow()
        except EditorControllerError as error:
            self._show_feedback(_controller_error_feedback(error), failed=True)
            return
        self._handle_outcome(
            outcome,
            preferred_source=draft.source,
            preferred_row=preferred_row,
        )

    def _delete_term(self) -> None:
        record = self._selected_record
        if record is None or self._creating:
            self._show_feedback("请先选择要删除的术语。", failed=True)
            return
        if not self._confirm_delete(record):
            return
        previous_row = self.term_table.currentRow()
        try:
            outcome = self.controller.delete_term(
                self.resource_id,
                record.locator,
            )
        except EditorControllerError as error:
            self._show_feedback(_controller_error_feedback(error), failed=True)
            return
        self._handle_outcome(outcome, preferred_row=previous_row)

    def _confirm_delete(self, record: TermRecord) -> bool:
        answer = ask_localized_question(
            self,
            title="删除术语",
            text=f"确定删除“{record.source} → {record.target}”吗？",
            buttons=(
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel
            ),
            default_button=QMessageBox.StandardButton.Cancel,
            button_labels={
                QMessageBox.StandardButton.Yes: "删除",
                QMessageBox.StandardButton.Cancel: "取消",
            },
        )
        return answer == QMessageBox.StandardButton.Yes

    def _handle_outcome(
        self,
        outcome: TermCommitOutcome,
        *,
        preferred_source: str | None = None,
        preferred_row: int | None = None,
    ) -> None:
        if type(outcome) is not TermCommitOutcome:
            raise TypeError("term mutation must return TermCommitOutcome")
        outcome.__post_init__()
        if outcome.state is TermCommitState.COMMITTED:
            self._matcher_display = self._read_matcher_display()
            self._load_terms(
                preferred_source=preferred_source,
                preferred_row=preferred_row,
            )
            self._show_feedback("术语变更已保存。", failed=False)
            self.terms_committed.emit()
            return

        error_code = (
            outcome.error_code
            if outcome.error_code in _TERM_COMMIT_ERROR_CODES
            else "TERM_COMMIT_FAILED"
        )
        details = [
            f"术语变更未发布：{outcome.state.value}",
            f"错误代码：{error_code}",
        ]
        if outcome.recovery_path is not None:
            details.append(f"恢复文件：{outcome.recovery_path}")
        if outcome.quarantined:
            details.append("资源已隔离；恢复前不得重试变更。")
        elif outcome.retryable:
            details.append("原状态可用；根据上述指引后可重试。")
        self._show_feedback("\n".join(details), failed=True)

    def _show_feedback(self, message: str, *, failed: bool) -> None:
        self.feedback_label.setText(message)
        self.feedback_label.setProperty("failed", failed)
        self.feedback_label.style().unpolish(self.feedback_label)
        self.feedback_label.style().polish(self.feedback_label)


_TERM_DIALOG_STYLE = """
QDialog#termbaseDialog {
    background: #f3f6fa;
    color: #182233;
    font-family: "Inter", "Noto Sans CJK SC", sans-serif;
    font-size: 13px;
}
QLabel#termDialogTitle {
    color: #10243b;
    font-size: 21px;
    font-weight: 700;
}
QLabel#termResourceName, QLabel#termMatcherCapability {
    color: #52677b;
}
QFrame#termEditorPanel {
    background: #ffffff;
    border: 1px solid #d7e0ea;
    border-radius: 7px;
}
QTableWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fc;
    border: 1px solid #d7e0ea;
}
QHeaderView::section {
    background: #eaf0f6;
    color: #53657a;
    border: none;
    border-bottom: 1px solid #d7e0ea;
    padding: 7px;
    font-weight: 700;
}
QLineEdit {
    min-height: 30px;
    border: 1px solid #cbd6e1;
    border-radius: 5px;
    padding: 2px 8px;
    background: #ffffff;
}
QPushButton {
    min-height: 30px;
    padding: 2px 12px;
    border: 1px solid #c6d2df;
    border-radius: 5px;
    background: #ffffff;
    color: #26435e;
    font-weight: 600;
}
QPushButton#saveTerm {
    background: #079fca;
    border-color: #079fca;
    color: #ffffff;
}
QLabel#termFeedback {
    color: #2f6b50;
}
QLabel#termFeedback[failed="true"] {
    color: #a13434;
}
"""
