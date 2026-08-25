"""Controller-only Qt dialog for target literal preprocessing."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from editor_contracts import (
    BatchOperationReport,
    LiteralReplaceRule,
    PreprocessPreferences,
    PreprocessPreview,
)
from editor_controller import EditorController, EditorControllerError
from qt_localized_message_box import ask_localized_question


_ERROR_MESSAGES = {
    "PREPROCESS.EMPTY_FIND": "查找文字不能为空。",
    "PREPROCESS.NO_ENABLED_RULES": "请至少启用一条文字规则。",
    "PREPROCESS.NO_SELECTED_STATUS": "请至少选择“草稿”或“已确认”中的一种状态。",
    "PREPROCESS.INVALID_STATUS_SELECTION": "段落状态筛选无效，请重新选择。",
    "PREPROCESS.NO_CHANGES": "当前规则没有产生可应用的 target 修改。",
    "PREPROCESS.INVALID_RULES": "规则内容无效；请检查查找文字与启用状态。",
    "PREPROCESS.PREVIEW_INVALID": "预览数据无效，请重新预览。",
    "PREPROCESS.STALE_PROJECT_SESSION": "项目已切换，请关闭窗口后重新打开。",
    "PREPROCESS.STALE_REVISION": "项目内容已变化，请重新预览。",
    "PREPROCESS.STALE_SEGMENT": "预览涉及的段落已变化，请重新预览。",
    "PREPROCESS.NO_UNDO": "当前项目没有可撤销的批量预处理。",
    "PREPROCESS.STALE_UNDO_SESSION": "撤销点不属于当前项目。",
    "PREPROCESS.STALE_UNDO": "批次涉及的段落后来已被编辑，未执行任何撤销。",
    "PREPROCESS.PREFERENCES_SAVE_FAILED": "规则保存失败；已保留上次成功保存的偏好和当前项目。",
    "PREPROCESS.PREFERENCES_READ_FAILED": "无法读取已保存的预处理规则；已使用安全默认值。",
    "PREPROCESS.PREFERENCES_INVALID": "已保存的预处理规则无效；已使用安全默认值。",
    "PROJECT_TOOLS.JSON_REQUIRED": "文字预处理仅适用于当前单个 JSON 项目。",
    "PROJECT_TOOLS.NO_PROJECT": "请先打开一个 JSON 项目。",
}


class QtPreprocessDialog(QDialog):
    """Edit ordered literal rules, preview, apply and undo via Controller."""

    mutation_committed = Signal(object)

    def __init__(
        self,
        controller: EditorController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._preview: PreprocessPreview | None = None
        self._updating_rules = False
        self.setObjectName("preprocessDialog")
        self.setWindowTitle("Target 文字预处理")
        self.setMinimumSize(820, 580)
        self.setAccessibleName("Target 文字预处理")
        self._build_ui()
        restore_error: str | None = None
        try:
            preferences = self.controller.preprocess_preferences()
        except (EditorControllerError, TypeError, ValueError) as error:
            preferences = PreprocessPreferences()
            restore_error = self._friendly_error(error)
        self._restore_preferences(preferences)
        self._connect_actions()
        self._update_undo_button()
        if restore_error is not None:
            self._set_status(restore_error, error=True)
        elif preferences.rules:
            self._set_status("已恢复保存的规则；预览不会修改项目。", error=False)
        else:
            self._set_status("预览不会修改项目。", error=False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        introduction = QLabel(
            "规则按下列顺序进行区分大小写的普通文字替换；只处理 target，"
            "不支持正则或脚本。"
        )
        introduction.setWordWrap(True)
        introduction.setObjectName("preprocessIntroduction")
        layout.addWidget(introduction)

        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setObjectName("preprocessRulesTable")
        self.rules_table.setAccessibleName("有序文字替换规则")
        self.rules_table.setHorizontalHeaderLabels(("启用", "查找", "替换为"))
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.rules_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.rules_table, 2)

        rule_actions = QHBoxLayout()
        self.add_rule_button = QPushButton("添加规则")
        self.add_rule_button.setObjectName("preprocessAddRule")
        self.add_rule_button.setToolTip("在末尾添加一条 literal 规则")
        self.remove_rule_button = QPushButton("删除规则")
        self.remove_rule_button.setObjectName("preprocessRemoveRule")
        self.remove_rule_button.setToolTip("删除当前选中的规则")
        self.move_rule_up_button = QPushButton("上移")
        self.move_rule_up_button.setObjectName("preprocessMoveRuleUp")
        self.move_rule_up_button.setToolTip("提高当前规则的应用顺序")
        self.move_rule_down_button = QPushButton("下移")
        self.move_rule_down_button.setObjectName("preprocessMoveRuleDown")
        self.move_rule_down_button.setToolTip("降低当前规则的应用顺序")
        self.save_rules_button = QPushButton("保存规则")
        self.save_rules_button.setObjectName("preprocessSaveRules")
        self.save_rules_button.setToolTip(
            "保存规则顺序、启用状态和段落状态筛选；不会运行规则"
        )
        for button in (
            self.add_rule_button,
            self.remove_rule_button,
            self.move_rule_up_button,
            self.move_rule_down_button,
        ):
            rule_actions.addWidget(button)
        rule_actions.addStretch()
        rule_actions.addWidget(self.save_rules_button)
        layout.addLayout(rule_actions)

        status_filters = QHBoxLayout()
        status_filters.addWidget(QLabel("应用到："))
        self.include_draft_checkbox = QCheckBox("草稿")
        self.include_draft_checkbox.setObjectName("preprocessIncludeDraft")
        self.include_draft_checkbox.setAccessibleName("预处理状态：草稿")
        self.include_draft_checkbox.setToolTip(
            "包含 confirmed=false 的 target 段落"
        )
        self.include_confirmed_checkbox = QCheckBox("已确认")
        self.include_confirmed_checkbox.setObjectName("preprocessIncludeConfirmed")
        self.include_confirmed_checkbox.setAccessibleName("预处理状态：已确认")
        self.include_confirmed_checkbox.setToolTip(
            "包含 confirmed=true 的 target 段落；变化后将设为待确认"
        )
        status_filters.addWidget(self.include_draft_checkbox)
        status_filters.addWidget(self.include_confirmed_checkbox)
        status_filters.addStretch()
        layout.addLayout(status_filters)

        preview_heading = QHBoxLayout()
        self.preview_count_label = QLabel("尚未预览")
        self.preview_count_label.setObjectName("preprocessPreviewCount")
        self.preview_count_label.setAccessibleName("预处理预览状态：尚未预览")
        preview_heading.addWidget(self.preview_count_label)
        preview_heading.addStretch()
        self.preview_button = QPushButton("预览")
        self.preview_button.setObjectName("preprocessPreview")
        self.preview_button.setToolTip("只读计算受影响段落和修改前后内容")
        self.cancel_preview_button = QPushButton("取消预览")
        self.cancel_preview_button.setObjectName("preprocessCancelPreview")
        self.cancel_preview_button.setEnabled(False)
        self.cancel_preview_button.setToolTip("丢弃当前预览，不修改项目")
        preview_heading.addWidget(self.preview_button)
        preview_heading.addWidget(self.cancel_preview_button)
        layout.addLayout(preview_heading)

        self.preview_table = QTableWidget(0, 3)
        self.preview_table.setObjectName("preprocessPreviewTable")
        self.preview_table.setAccessibleName("预处理修改前后预览")
        self.preview_table.setHorizontalHeaderLabels(("段落", "修改前", "修改后"))
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        preview_header = self.preview_table.horizontalHeader()
        preview_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        preview_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        preview_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.preview_table, 3)

        self.status_label = QLabel("预览不会修改项目。")
        self.status_label.setObjectName("preprocessStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("预处理操作状态：预览不会修改项目")
        layout.addWidget(self.status_label)

        operation_actions = QHBoxLayout()
        self.undo_button = QPushButton("撤销最近一次应用")
        self.undo_button.setObjectName("preprocessUndoLatest")
        self.undo_button.setToolTip("恢复最近批次涉及段落的 target 和确认状态")
        self.apply_button = QPushButton("应用预览")
        self.apply_button.setObjectName("preprocessApply")
        self.apply_button.setEnabled(False)
        self.apply_button.setToolTip("确认后原子应用当前预览")
        operation_actions.addWidget(self.undo_button)
        operation_actions.addStretch()
        operation_actions.addWidget(self.apply_button)
        layout.addLayout(operation_actions)

        self.close_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close
        )
        self.close_buttons.setObjectName("preprocessCloseButtons")
        self.close_buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "关闭"
        )
        layout.addWidget(self.close_buttons)

    def _connect_actions(self) -> None:
        self.add_rule_button.clicked.connect(
            lambda: self._append_rule_row(find="", replacement="", enabled=True)
        )
        self.remove_rule_button.clicked.connect(self._remove_selected_rule)
        self.move_rule_up_button.clicked.connect(lambda: self._move_selected_rule(-1))
        self.move_rule_down_button.clicked.connect(
            lambda: self._move_selected_rule(1)
        )
        self.save_rules_button.clicked.connect(self.save_preferences)
        self.rules_table.itemChanged.connect(self._rules_changed)
        self.include_draft_checkbox.toggled.connect(self._status_filter_changed)
        self.include_confirmed_checkbox.toggled.connect(self._status_filter_changed)
        self.preview_button.clicked.connect(self.preview_rules)
        self.cancel_preview_button.clicked.connect(self.cancel_preview)
        self.apply_button.clicked.connect(self.apply_preview)
        self.undo_button.clicked.connect(self.undo_latest)
        self.close_buttons.rejected.connect(self.reject)

    def _restore_preferences(self, preferences: PreprocessPreferences) -> None:
        self.include_draft_checkbox.setChecked(preferences.include_draft)
        self.include_confirmed_checkbox.setChecked(preferences.include_confirmed)
        if preferences.rules:
            for rule in preferences.rules:
                self._append_rule_row(
                    find=rule.find,
                    replacement=rule.replacement,
                    enabled=rule.enabled,
                )
        else:
            self._append_rule_row(find="", replacement="", enabled=True)

    def _append_rule_row(
        self,
        *,
        find: str,
        replacement: str,
        enabled: bool,
    ) -> None:
        self._updating_rules = True
        try:
            row = self.rules_table.rowCount()
            self.rules_table.insertRow(row)
            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            enabled_item.setCheckState(
                Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
            )
            enabled_item.setToolTip("是否按当前顺序应用此规则")
            find_item = QTableWidgetItem(find)
            find_item.setToolTip("区分大小写的普通查找文字；不能为空")
            replacement_item = QTableWidgetItem(replacement)
            replacement_item.setToolTip("普通替换文字；可以为空")
            self.rules_table.setItem(row, 0, enabled_item)
            self.rules_table.setItem(row, 1, find_item)
            self.rules_table.setItem(row, 2, replacement_item)
            self.rules_table.setCurrentCell(row, 1)
        finally:
            self._updating_rules = False
        self._invalidate_preview("规则已变化，请重新预览。")

    def rules(self) -> tuple[LiteralReplaceRule, ...]:
        """Read the visible rows in their exact application order."""

        rules: list[LiteralReplaceRule] = []
        for row in range(self.rules_table.rowCount()):
            enabled_item = self.rules_table.item(row, 0)
            find_item = self.rules_table.item(row, 1)
            replacement_item = self.rules_table.item(row, 2)
            if enabled_item is None or find_item is None or replacement_item is None:
                raise ValueError("规则表包含不完整的行。")
            if not find_item.text():
                raise EditorControllerError("PREPROCESS.EMPTY_FIND")
            rules.append(
                LiteralReplaceRule(
                    find=find_item.text(),
                    replacement=replacement_item.text(),
                    enabled=enabled_item.checkState() == Qt.CheckState.Checked,
                )
            )
        return tuple(rules)

    def _rule_rows(self) -> tuple[tuple[bool, str, str], ...]:
        rows: list[tuple[bool, str, str]] = []
        for row in range(self.rules_table.rowCount()):
            enabled = self.rules_table.item(row, 0)
            find = self.rules_table.item(row, 1)
            replacement = self.rules_table.item(row, 2)
            if enabled is None or find is None or replacement is None:
                continue
            rows.append(
                (
                    enabled.checkState() == Qt.CheckState.Checked,
                    find.text(),
                    replacement.text(),
                )
            )
        return tuple(rows)

    def _rebuild_rule_rows(
        self,
        rows: tuple[tuple[bool, str, str], ...],
        selected_row: int,
    ) -> None:
        self._updating_rules = True
        try:
            self.rules_table.setRowCount(0)
        finally:
            self._updating_rules = False
        for enabled, find, replacement in rows:
            self._append_rule_row(
                find=find,
                replacement=replacement,
                enabled=enabled,
            )
        if rows:
            self.rules_table.setCurrentCell(selected_row, 1)
        self._invalidate_preview("规则顺序已变化，请重新预览。")

    def _remove_selected_rule(self) -> None:
        row = self.rules_table.currentRow()
        if row < 0:
            return
        self._updating_rules = True
        try:
            self.rules_table.removeRow(row)
        finally:
            self._updating_rules = False
        self._invalidate_preview("规则已变化，请重新预览。")

    def _move_selected_rule(self, direction: int) -> None:
        row = self.rules_table.currentRow()
        destination = row + direction
        rows = list(self._rule_rows())
        if row < 0 or destination < 0 or destination >= len(rows):
            return
        rows[row], rows[destination] = rows[destination], rows[row]
        self._rebuild_rule_rows(tuple(rows), destination)

    def _rules_changed(self, _item: QTableWidgetItem) -> None:
        if not self._updating_rules:
            self._invalidate_preview("规则已变化，请重新预览。")

    def _status_filter_changed(self, _checked: bool) -> None:
        self._invalidate_preview("段落状态筛选已变化，请重新预览。")

    def save_preferences(self) -> bool:
        """Persist visible rules and filters without previewing or mutating."""

        try:
            if not (
                self.include_draft_checkbox.isChecked()
                or self.include_confirmed_checkbox.isChecked()
            ):
                raise EditorControllerError("PREPROCESS.NO_SELECTED_STATUS")
            preferences = PreprocessPreferences(
                rules=self.rules(),
                include_draft=self.include_draft_checkbox.isChecked(),
                include_confirmed=self.include_confirmed_checkbox.isChecked(),
            )
            self.controller.update_preprocess_preferences(preferences)
        except (EditorControllerError, TypeError, ValueError) as error:
            self._set_status(self._friendly_error(error), error=True)
            return False
        self._set_status(
            "已保存规则和段落状态筛选；未运行规则，项目未修改。",
            error=False,
        )
        return True

    def _invalidate_preview(self, message: str) -> None:
        self._preview = None
        self.preview_table.setRowCount(0)
        self.preview_count_label.setText("尚未预览")
        self.preview_count_label.setAccessibleName("预处理预览状态：尚未预览")
        self.apply_button.setEnabled(False)
        self.cancel_preview_button.setEnabled(False)
        self._set_status(message, error=False)

    def cancel_preview(self) -> None:
        """Discard the dialog-owned immutable preview without mutation."""

        self._invalidate_preview("已取消预览；项目未修改。")

    def preview_rules(self) -> bool:
        """Request a fresh read-only preview from the Controller."""

        try:
            rules = self.rules()
            preview = self.controller.preview_preprocessing(
                rules,
                include_draft=self.include_draft_checkbox.isChecked(),
                include_confirmed=self.include_confirmed_checkbox.isChecked(),
            )
        except (EditorControllerError, TypeError, ValueError) as error:
            self._invalidate_preview(self._friendly_error(error))
            self._set_status(self._friendly_error(error), error=True)
            return False

        self._preview = preview
        self.preview_table.setRowCount(len(preview.changes))
        for row, change in enumerate(preview.changes):
            values = (
                f"{change.segment_index + 1} · {change.segment_id}",
                change.before_target,
                change.after_target,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.preview_table.setItem(row, column, item)
        draft_count = sum(not change.before_confirmed for change in preview.changes)
        confirmed_count = sum(change.before_confirmed for change in preview.changes)
        count_text = (
            f"将影响 {len(preview.changes)} 个段落"
            f"（草稿 {draft_count}，已确认 {confirmed_count}）"
        )
        self.preview_count_label.setText(count_text)
        self.preview_count_label.setAccessibleName(
            f"预处理预览状态：{count_text}"
        )
        self.apply_button.setEnabled(True)
        self.cancel_preview_button.setEnabled(True)
        self._set_status("预览已生成；项目内容和确认状态尚未改变。", error=False)
        return True

    def apply_preview(self) -> bool:
        """Explicitly confirm and atomically apply the current preview."""

        preview = self._preview
        if preview is None:
            self._set_status("请先生成预览。", error=True)
            return False
        if not self._confirm_apply(
            len(preview.changes),
            any(change.before_confirmed for change in preview.changes),
        ):
            self._set_status("已取消应用；项目未修改。", error=False)
            return False
        try:
            report = self.controller.apply_preprocessing(preview)
        except (EditorControllerError, TypeError, ValueError) as error:
            self._invalidate_preview(self._friendly_error(error))
            self._set_status(self._friendly_error(error), error=True)
            self._update_undo_button()
            return False
        self._after_mutation(report, f"已应用 {len(report.changed_segment_ids)} 个段落。")
        return True

    def undo_latest(self) -> bool:
        """Undo the latest retained batch through the Controller."""

        try:
            report = self.controller.undo_latest_preprocessing()
        except (EditorControllerError, TypeError, ValueError) as error:
            self._set_status(self._friendly_error(error), error=True)
            self._update_undo_button()
            return False
        self._after_mutation(report, f"已撤销 {len(report.changed_segment_ids)} 个段落。")
        return True

    def _after_mutation(
        self,
        report: BatchOperationReport,
        message: str,
    ) -> None:
        self._preview = None
        self.preview_table.setRowCount(0)
        self.preview_count_label.setText("尚未预览")
        self.apply_button.setEnabled(False)
        self.cancel_preview_button.setEnabled(False)
        self._set_status(message, error=False)
        self._update_undo_button()
        self.mutation_committed.emit(report)

    def _update_undo_button(self) -> None:
        available = self.controller.has_preprocessing_undo
        self.undo_button.setEnabled(available)
        self.undo_button.setAccessibleName(
            "撤销最近一次批量预处理"
            if available
            else "撤销最近一次批量预处理：当前没有可撤销批次"
        )

    def _confirm_apply(
        self,
        affected_count: int,
        includes_confirmed: bool,
    ) -> bool:
        if includes_confirmed:
            prompt = (
                f"确定修改 {affected_count} 个段落的 target，"
                "并将变化段落设为待确认吗？"
            )
        else:
            prompt = f"确定修改 {affected_count} 个草稿段落的 target 吗？"
        decision = ask_localized_question(
            self,
            title="应用文字预处理",
            text=prompt,
            buttons=(
                QMessageBox.StandardButton.Apply
                | QMessageBox.StandardButton.Cancel
            ),
            default_button=QMessageBox.StandardButton.Cancel,
            button_labels={
                QMessageBox.StandardButton.Apply: "应用",
                QMessageBox.StandardButton.Cancel: "取消",
            },
        )
        return decision == QMessageBox.StandardButton.Apply

    def _friendly_error(self, error: BaseException) -> str:
        code = str(error)
        return _ERROR_MESSAGES.get(code, f"操作失败：{code}")

    def _set_status(self, message: str, *, error: bool) -> None:
        self.status_label.setText(message)
        self.status_label.setAccessibleName(f"预处理操作状态：{message}")
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
