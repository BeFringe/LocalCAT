"""Body-safe, preview-first Qt manager for collaborative Chunk plans."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from chunk_controller_contracts import (
    ChunkApplicationMode,
    ChunkApplicationMutationPreview,
    ChunkApplicationProjectView,
    ChunkApplicationRebaseInspection,
    ChunkApplicationSegmentChoice,
    ChunkApplicationSegmentSelectionRequest,
    ChunkApplicationSplitChild,
    SegmentIdentity,
)


_PRIMARY = (
    ("partition", "拆分项目"),
    ("split_evenly", "拆分分工"),
    ("merge", "合并分工"),
)
_ADVANCED = (
    ("create", "新建所选段落分工"),
    ("rename", "重命名"),
    ("reorder", "调整顺序"),
    ("exact_split", "精确拆分所选段落"),
    ("move", "移动所选段落"),
    ("release", "释放所选段落"),
    ("dissolve_chunk", "解散分工"),
    ("dissolve_plan", "解散全部分工"),
    ("assign", "分配给本机工作流"),
    ("reassign", "改派给本机工作流"),
    ("unassign", "取消分配"),
    ("rebase", "同步工作区变化"),
    ("undo", "撤销最近操作"),
)
_LABELS = dict(_PRIMARY + _ADVANCED)
_MAX_GROUPS = 4_096


class _CurrentPageStack(QStackedWidget):
    """Let the active operation form, rather than the tallest form, size the stack."""

    def sizeHint(self):  # noqa: N802 - Qt virtual name
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self):  # noqa: N802 - Qt virtual name
        current = self.currentWidget()
        return (
            current.minimumSizeHint()
            if current is not None
            else super().minimumSizeHint()
        )


class QtChunkManagerDialog(QDialog):
    mutationCommitted = Signal(object)
    viewChanged = Signal(object)
    viewRefreshRequested = Signal()
    mutation_committed = Signal(object)
    view_changed = Signal(object)
    view_refresh_requested = Signal()
    segmentSelectionRequested = Signal(object)
    segment_selection_requested = Signal(object)

    def __init__(self, facade: object, view: ChunkApplicationProjectView, parent=None):
        super().__init__(parent)
        if type(view) is not ChunkApplicationProjectView:
            raise TypeError("chunk manager requires an exact application view")
        view.__post_init__()
        if not callable(getattr(facade, "apply_mutation", None)):
            raise TypeError("chunk manager requires a mutation facade")
        self._facade = facade
        self._view = view
        self._segment_choices: tuple[ChunkApplicationSegmentChoice, ...] = ()
        self._rebase_inspection: ChunkApplicationRebaseInspection | None = None
        self._split_children: tuple[ChunkApplicationSplitChild, ...] = ()
        self._preview: ChunkApplicationMutationPreview | None = None
        self._refreshing = False
        self._current_action = "partition"
        self._chunk_selection_anchor_row: int | None = None
        self._selected_segment_identities: tuple[SegmentIdentity, ...] = ()
        self._pending_segment_selection_request: (
            ChunkApplicationSegmentSelectionRequest | None
        ) = None

        self.setObjectName("chunkManagerDialog")
        self.setWindowTitle("协作分工管理")
        self.setMinimumSize(820, 560)
        self.resize(1120, 760)
        self.setSizeGripEnabled(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        root.addLayout(self._build_header())
        self.body_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.body_splitter.setObjectName("chunkBodySplitter")
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.addWidget(self._build_scope_card())
        self.body_splitter.addWidget(self._build_operation_card())
        self.body_splitter.setStretchFactor(0, 5)
        self.body_splitter.setStretchFactor(1, 6)
        self.body_splitter.setSizes((480, 560))
        root.addWidget(self.body_splitter, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.setObjectName("chunkCloseButton")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        self.setStyleSheet(_STYLE)
        self._wire()
        self.refresh(view)

    def _build_header(self):
        row = QHBoxLayout()
        labels = QVBoxLayout()
        title = QLabel("协作分工")
        title.setObjectName("chunkManagerTitle")
        subtitle = QLabel("直接拆分项目或现有分工；预览确认后才发布。")
        subtitle.setObjectName("chunkManagerSubtitle")
        labels.addWidget(title)
        labels.addWidget(subtitle)
        row.addLayout(labels, 1)
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("chunkModeBadge")
        self.mode_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_badge.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        self.mode_badge.setMaximumHeight(36)
        row.addWidget(self.mode_badge)
        return row

    def _build_scope_card(self):
        card = QFrame()
        card.setObjectName("chunkCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        title = QLabel("分工与范围")
        title.setObjectName("chunkSectionTitle")
        layout.addWidget(title)
        self.project_summary = QLabel()
        self.project_summary.setObjectName("chunkProjectSummary")
        self.project_summary.setWordWrap(True)
        self.project_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.project_summary)
        chunk_box = QWidget()
        self.chunk_scope_box = chunk_box
        chunk_layout = QVBoxLayout(chunk_box)
        chunk_layout.setContentsMargins(0, 0, 0, 0)
        self.chunk_scope_heading = QLabel("分工范围")
        self.chunk_scope_heading.setObjectName("chunkSubsectionTitle")
        chunk_layout.addWidget(self.chunk_scope_heading)
        self.chunk_table = QTableWidget(0, 4)
        self.chunk_table.setObjectName("chunkOverviewTable")
        self.chunk_table.setHorizontalHeaderLabels(("分工", "段落", "进度", "分配"))
        self.chunk_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chunk_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.chunk_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.chunk_table.verticalHeader().setVisible(False)
        chunk_header = self.chunk_table.horizontalHeader()
        chunk_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        chunk_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        chunk_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        chunk_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.chunk_table.setAlternatingRowColors(True)
        self.chunk_table.viewport().installEventFilter(self)
        chunk_layout.addWidget(self.chunk_table, 1)
        chunk_scope_footer = QHBoxLayout()
        self.chunk_scope_hint = QLabel()
        self.chunk_scope_hint.setObjectName("chunkFormHint")
        self.chunk_scope_hint.setWordWrap(True)
        chunk_scope_footer.addWidget(self.chunk_scope_hint, 1)
        self.select_all_chunks_button = QPushButton("全选全部分工")
        self.select_all_chunks_button.setObjectName("chunkSelectAllChunks")
        self.select_all_chunks_button.setAccessibleName("选择当前计划的全部分工")
        self.select_all_chunks_button.hide()
        chunk_scope_footer.addWidget(self.select_all_chunks_button)
        chunk_layout.addLayout(chunk_scope_footer)

        layout.addWidget(chunk_box, 1)
        return card

    def _build_operation_card(self):
        card = QFrame()
        card.setObjectName("chunkCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        row = QHBoxLayout()
        title = QLabel("操作")
        title.setObjectName("chunkSectionTitle")
        row.addWidget(title)
        row.addStretch(1)
        self.action_combo = QComboBox()
        self.action_combo.setObjectName("chunkActionCombo")
        self.action_combo.setMinimumWidth(170)
        self.action_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.action_combo.setMinimumContentsLength(8)
        row.addWidget(self.action_combo)
        layout.addLayout(row)
        advanced = QHBoxLayout()
        self.advanced_button = QPushButton("高级操作")
        self.advanced_button.setObjectName("chunkAdvancedButton")
        self.advanced_button.setCheckable(True)
        self.advanced_combo = QComboBox()
        self.advanced_combo.setObjectName("chunkAdvancedCombo")
        self.advanced_combo.setMinimumWidth(280)
        self.advanced_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        for key, label in _ADVANCED:
            self.advanced_combo.addItem(label, key)
        self.advanced_combo.hide()
        advanced.addWidget(self.advanced_button)
        advanced.addWidget(self.advanced_combo, 1)
        advanced.addStretch(1)
        layout.addLayout(advanced)
        self.advanced_help = QLabel(
            "高级操作：先选择分工；需要精确段落时会进入浏览 / 校对页，"
            "返回后再生成预览。"
        )
        self.advanced_help.setObjectName("chunkAdvancedHelp")
        self.advanced_help.setWordWrap(True)
        self.advanced_help.hide()
        layout.addWidget(self.advanced_help)
        self.segment_selection_panel = QFrame()
        self.segment_selection_panel.setObjectName("chunkSegmentSelectionPanel")
        segment_selection_layout = QHBoxLayout(self.segment_selection_panel)
        segment_selection_layout.setContentsMargins(12, 9, 12, 9)
        segment_selection_layout.setSpacing(10)
        self.selection_summary = QLabel("尚未选择段落")
        self.selection_summary.setObjectName("chunkSelectionSummary")
        self.selection_summary.setWordWrap(True)
        segment_selection_layout.addWidget(self.selection_summary, 1)
        self.segment_selection_button = QPushButton("在浏览 / 校对中选择段落…")
        self.segment_selection_button.setObjectName("chunkOpenBrowseSelection")
        self.segment_selection_button.setAccessibleName(
            "在浏览校对页选择高级操作段落"
        )
        segment_selection_layout.addWidget(self.segment_selection_button)
        self.segment_selection_panel.hide()
        layout.addWidget(self.segment_selection_panel)
        self.operation_pages = _CurrentPageStack()
        self.operation_pages.setObjectName("chunkOperationPages")
        self.operation_pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._pages = {}
        for key, _label in _PRIMARY + _ADVANCED:
            self._pages[key] = self.operation_pages.addWidget(self._build_page(key))
        layout.addWidget(self.operation_pages)
        preview_title = QLabel("发布预览")
        preview_title.setObjectName("chunkSectionTitle")
        layout.addWidget(preview_title)
        self.preview_panel = QLabel("选择范围并生成预览。")
        self.preview_panel.setObjectName("chunkPreviewPanel")
        self.preview_panel.setWordWrap(True)
        self.preview_panel.setTextFormat(Qt.TextFormat.RichText)
        self.preview_panel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setObjectName("chunkPreviewScroll")
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(120)
        scroll.setWidget(self.preview_panel)
        layout.addWidget(scroll, 1)
        self.confirm_check = QCheckBox("我已核对预览并确认执行")
        self.confirm_check.setObjectName("chunkConfirmCheck")
        self.confirm_check.setEnabled(False)
        layout.addWidget(self.confirm_check)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("生成预览")
        self.preview_button.setObjectName("chunkPreviewButton")
        self.apply_button = QPushButton("确认发布")
        self.apply_button.setObjectName("chunkApplyButton")
        self.apply_button.setEnabled(False)
        buttons.addWidget(self.preview_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        return card

    @staticmethod
    def _form():
        page = QWidget()
        form = QGridLayout(page)
        form.setContentsMargins(0, 4, 0, 4)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)
        form.setColumnStretch(1, 1)
        form.setAlignment(Qt.AlignmentFlag.AlignTop)
        return page, form

    @staticmethod
    def _hint(text):
        label = QLabel(text)
        label.setObjectName("chunkFormHint")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _combo(name):
        combo = QComboBox()
        combo.setObjectName(name)
        return combo

    def _group_editor(self, prefix):
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        count = QSpinBox()
        count.setObjectName(prefix + "GroupCount")
        count.setRange(2, 2)
        names = QListWidget()
        names.setObjectName(prefix + "GroupNames")
        names.setMinimumHeight(68)
        names.setMaximumHeight(96)
        grid.addWidget(QLabel("分组数"), 0, 0)
        grid.addWidget(count, 0, 1)
        grid.addWidget(QLabel("分工名称（双击可编辑）"), 1, 0, 1, 2)
        grid.addWidget(names, 2, 0, 1, 2)
        return box, count, names

    def _build_page(self, key):
        page, form = self._form()
        if key == "partition":
            box, self.partition_group_count, self.partition_group_names = self._group_editor("chunkPartition")
            self.partition_scope = self._hint("将全项目段落按顺序平衡拆分。")
            form.addWidget(self.partition_scope, 0, 0, 1, 2)
            form.addWidget(box, 1, 0, 1, 2)
        elif key == "split_evenly":
            self.split_even_scope = self._hint("请在左侧选择一个原分工。")
            box, self.split_group_count, self.split_group_names = self._group_editor("chunkSplitEven")
            self.split_assignment = self._combo("chunkSplitAssignment")
            self.split_assignment.addItem("请选择分配处理", None)
            self.split_assignment.addItem("继承原分工分配", "inherit")
            self.split_assignment.addItem("拆分后取消分配", "unassign")
            form.addWidget(self.split_even_scope, 0, 0, 1, 2)
            form.addWidget(box, 1, 0, 1, 2)
            form.addWidget(QLabel("分配处理"), 2, 0)
            form.addWidget(self.split_assignment, 2, 1)
        elif key == "merge":
            self.merge_scope = self._hint("请在左侧选择至少两个源分工。")
            self.merge_name = QLineEdit()
            self.merge_name.setObjectName("chunkMergeName")
            self.merge_name.setPlaceholderText("留空则使用“合并分工”")
            self.merge_assignment = self._combo("chunkMergeAssignment")
            self.merge_assignment.addItem("保留原分配（需一致）", None)
            self.merge_assignment.addItem("分配给本机工作流", True)
            self.merge_assignment.addItem("明确不分配", False)
            form.addWidget(self.merge_scope, 0, 0, 1, 2)
            form.addWidget(QLabel("结果名称（可选）"), 1, 0)
            form.addWidget(self.merge_name, 1, 1)
            form.addWidget(QLabel("结果分配"), 2, 0)
            form.addWidget(self.merge_assignment, 2, 1)
        elif key == "create":
            self.create_name = QLineEdit()
            self.create_name.setObjectName("chunkCreateName")
            form.addWidget(QLabel("分工名称"), 0, 0)
            form.addWidget(self.create_name, 0, 1)
            form.addWidget(
                self._hint("成员从浏览 / 校对页的完整双语上下文中选择。"),
                1,
                0,
                1,
                2,
            )
        elif key == "rename":
            self.rename_chunk = self._combo("chunkRenameSource")
            self.rename_name = QLineEdit()
            self.rename_name.setObjectName("chunkRenameName")
            form.addWidget(QLabel("分工"), 0, 0)
            form.addWidget(self.rename_chunk, 0, 1)
            form.addWidget(QLabel("新名称"), 1, 0)
            form.addWidget(self.rename_name, 1, 1)
        elif key == "reorder":
            self.reorder_list = QListWidget()
            self.reorder_list.setObjectName("chunkReorderList")
            self.reorder_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            self.reorder_list.setMaximumHeight(120)
            form.addWidget(QLabel("拖动排序"), 0, 0)
            form.addWidget(self.reorder_list, 0, 1)
        elif key == "exact_split":
            self.split_first_name = QLineEdit()
            self.split_first_name.setObjectName("chunkSplitFirstName")
            self.split_second_name = QLineEdit()
            self.split_second_name.setObjectName("chunkSplitSecondName")
            self.split_cut = QSpinBox()
            self.split_cut.setObjectName("chunkSplitCut")
            self.split_cut.setRange(1, 1)
            self.exact_split_assignment = self._combo("chunkExactSplitAssignment")
            self.exact_split_assignment.addItem("请选择分配处理", None)
            self.exact_split_assignment.addItem("继承原分工分配", "inherit")
            self.exact_split_assignment.addItem("拆分后取消分配", "unassign")
            self.split_summary = self._hint(
                "左侧选一个原分工，再到浏览 / 校对页选择其成员。"
            )
            for row, (label, widget) in enumerate((("第一组名称", self.split_first_name), ("第二组名称", self.split_second_name), ("第一组段落数", self.split_cut), ("分配处理", self.exact_split_assignment))):
                form.addWidget(QLabel(label), row, 0)
                form.addWidget(widget, row, 1)
            form.addWidget(self.split_summary, 4, 0, 1, 2)
        elif key == "move":
            self.move_source = self._combo("chunkMoveSource")
            self.move_destination = self._combo("chunkMoveDestination")
            self.move_retire = QCheckBox("若源分工为空，同时退役")
            form.addWidget(QLabel("源分工"), 0, 0)
            form.addWidget(self.move_source, 0, 1)
            form.addWidget(QLabel("目标分工"), 1, 0)
            form.addWidget(self.move_destination, 1, 1)
            form.addWidget(self.move_retire, 2, 0, 1, 2)
        elif key == "release":
            self.release_source = self._combo("chunkReleaseSource")
            self.release_retire = QCheckBox("若分工为空，同时退役")
            form.addWidget(QLabel("源分工"), 0, 0)
            form.addWidget(self.release_source, 0, 1)
            form.addWidget(self.release_retire, 1, 0, 1, 2)
        elif key == "dissolve_chunk":
            self.dissolve_chunk = self._combo("chunkDissolveSource")
            form.addWidget(QLabel("分工"), 0, 0)
            form.addWidget(self.dissolve_chunk, 0, 1)
        elif key == "dissolve_plan":
            form.addWidget(self._hint("解散计划；项目文档与翻译内容不受影响。"), 0, 0, 1, 2)
        elif key in {"assign", "reassign", "unassign"}:
            combo = self._combo("chunkAssignmentSource")
            setattr(self, key + "_chunk", combo)
            form.addWidget(QLabel("分工"), 0, 0)
            form.addWidget(combo, 0, 1)
        elif key == "rebase":
            self.rebase_summary = self._hint("检查工作区变化后选择显式处理方式。")
            self.rebase_missing_decision = self._combo("chunkRebaseMissingDecision")
            self.rebase_missing_decision.addItem("请选择缺失成员处理", None)
            self.rebase_missing_decision.addItem("释放已缺失成员", "release")
            self.rebase_empty_decision = self._combo("chunkRebaseEmptyDecision")
            self.rebase_empty_decision.addItem("请选择空分工处理", None)
            self.rebase_empty_decision.addItem("保留空分工", "keep")
            self.rebase_empty_decision.addItem("退役空分工", "retire")
            self.rebase_empty_decision.addItem("解散整个分工计划", "dissolve")
            form.addWidget(self.rebase_summary, 0, 0, 1, 2)
            form.addWidget(QLabel("缺失成员"), 1, 0)
            form.addWidget(self.rebase_missing_decision, 1, 1)
            form.addWidget(QLabel("空分工"), 2, 0)
            form.addWidget(self.rebase_empty_decision, 2, 1)
        elif key == "undo":
            form.addWidget(self._hint("撤销当前头部的最近分工操作。"), 0, 0, 1, 2)
        return page

    def _wire(self):
        self.action_combo.setAccessibleName("主要分工操作")
        self.advanced_button.setAccessibleName("显示高级分工操作")
        self.preview_button.setAccessibleName("生成分工发布预览")
        self.apply_button.setAccessibleName("确认发布分工变更")
        self.chunk_table.setAccessibleName("分工范围")
        self.partition_group_count.setAccessibleName("项目拆分份数")
        self.partition_group_names.setAccessibleName("项目拆分名称")
        self.split_group_count.setAccessibleName("分工拆分份数")
        self.split_group_names.setAccessibleName("分工拆分名称")
        self.split_assignment.setAccessibleName("拆分后分配处理")
        self.merge_name.setAccessibleName("合并后分工名称")
        self.action_combo.currentIndexChanged.connect(self._primary_changed)
        self.advanced_button.toggled.connect(self._advanced_toggled)
        self.advanced_combo.currentIndexChanged.connect(self._advanced_changed)
        self.preview_button.clicked.connect(self._preview_selected_action)
        self.apply_button.clicked.connect(self._apply_preview)
        self.confirm_check.toggled.connect(self._update_apply_enabled)
        for widget in self.findChildren(QLineEdit):
            widget.textChanged.connect(self._invalidate_preview)
        for widget in self.findChildren(QComboBox):
            if widget not in {self.action_combo, self.advanced_combo}:
                widget.currentIndexChanged.connect(self._invalidate_preview)
        for widget in self.findChildren(QCheckBox):
            if widget is not self.confirm_check:
                widget.toggled.connect(self._invalidate_preview)
        for widget in self.findChildren(QSpinBox):
            widget.valueChanged.connect(self._invalidate_preview)
        self.partition_group_count.valueChanged.connect(
            lambda count: self._sync_names(self.partition_group_names, count)
        )
        self.split_group_count.valueChanged.connect(
            lambda count: self._sync_names(self.split_group_names, count)
        )
        self.partition_group_names.itemChanged.connect(self._invalidate_preview)
        self.split_group_names.itemChanged.connect(self._invalidate_preview)
        self.reorder_list.model().rowsMoved.connect(self._invalidate_preview)
        self.chunk_table.itemSelectionChanged.connect(self._scope_changed)
        self.select_all_chunks_button.clicked.connect(self._select_all_chunks)
        self.segment_selection_button.clicked.connect(
            self._request_segment_selection
        )
        self.move_source.currentIndexChanged.connect(
            self._segment_selection_context_changed
        )
        self.release_source.currentIndexChanged.connect(
            self._segment_selection_context_changed
        )
        self._sync_names(self.partition_group_names, 2)
        self._sync_names(self.split_group_names, 2)

    def refresh(self, view: ChunkApplicationProjectView):
        if type(view) is not ChunkApplicationProjectView:
            raise TypeError("chunk manager refresh requires an exact view")
        view.__post_init__()
        self._view = view
        self._refreshing = True
        try:
            label = {
                ChunkApplicationMode.NO_PLAN: "○ 尚未分工",
                ChunkApplicationMode.ACTIVE: f"● 已启用 · {len(view.chunks)} 个分工",
                ChunkApplicationMode.BLOCKED: "● 需要处理后发布",
            }[view.mode]
            self.mode_badge.setText(label)
            self.mode_badge.setProperty("mode", view.mode.value)
            self.mode_badge.style().unpolish(self.mode_badge)
            self.mode_badge.style().polish(self.mode_badge)
            revision = "—" if view.plan_revision is None else str(view.plan_revision)
            safe = "" if view.safe_code is None else " · " + view.safe_code
            self.project_summary.setText(
                f"项目 {view.project_id}\n版本 {revision} · 尚未分工 {view.unallocated_count}{safe}"
            )
            self._load_segment_choices()
            self._chunk_selection_anchor_row = None
            self._selected_segment_identities = ()
            self._pending_segment_selection_request = None
            self._load_rebase_inspection()
            self._populate_chunks()
            self._populate_controls()
            self._configure_actions()
        finally:
            self._refreshing = False
        self._select_default_chunk()
        self._scope_changed()
        self._invalidate_preview()

    def _load_segment_choices(self):
        provider = getattr(self._facade, "segment_choices", None)
        if not callable(provider):
            self._segment_choices = ()
            return
        choices = provider()
        if type(choices) is not tuple or any(
            type(choice) is not ChunkApplicationSegmentChoice for choice in choices
        ):
            raise TypeError("CHUNK.SEGMENT_CHOICES_INVALID")
        for choice in choices:
            choice.__post_init__()
        self._segment_choices = choices

    def _load_rebase_inspection(self):
        self._rebase_inspection = None
        if not (
            self._view.mode is ChunkApplicationMode.BLOCKED
            and self._view.safe_code == "CHUNK.REBASE_REQUIRED"
        ):
            return
        provider = getattr(self._facade, "inspect_workspace_rebase", None)
        if not callable(provider):
            return
        inspection = provider()
        if type(inspection) is not ChunkApplicationRebaseInspection:
            raise TypeError("CHUNK.REBASE_INSPECTION_INVALID")
        inspection.__post_init__()
        self._rebase_inspection = inspection
        self.rebase_summary.setText(
            f"缺失成员 {len(inspection.missing_members)} · "
            f"新未分配 {inspection.new_unallocated_count} · "
            f"空分工 {len(inspection.empty_chunk_ids)}"
        )
        self.rebase_missing_decision.setEnabled(bool(inspection.missing_members))
        if not inspection.missing_members:
            self.rebase_missing_decision.setCurrentIndex(
                self.rebase_missing_decision.findData("release")
            )
        self.rebase_empty_decision.setEnabled(bool(inspection.empty_chunk_ids))
        if not inspection.empty_chunk_ids:
            self.rebase_empty_decision.setCurrentIndex(
                self.rebase_empty_decision.findData("keep")
            )
        self.rebase_empty_decision.model().item(
            self.rebase_empty_decision.findData("dissolve")
        ).setEnabled(inspection.all_chunks_empty)

    def _populate_chunks(self):
        if not self._view.chunks:
            attached = sum(choice.attached for choice in self._segment_choices)
            self.chunk_scope_heading.setText("项目范围")
            self.chunk_table.setRowCount(1)
            for column, value in enumerate(("整个项目", str(attached), "尚未拆分", "未分配")):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.chunk_table.setItem(0, column, item)
            return
        self.chunk_scope_heading.setText("分工范围")
        self.chunk_table.setRowCount(len(self._view.chunks))
        for row, chunk in enumerate(self._view.chunks):
            progress = chunk.progress
            values = (
                chunk.name,
                str(chunk.member_count),
                f"{progress.confirmed}/{progress.attached_total} 已确认"
                + (f" · {progress.detached} 已脱离" if progress.detached else ""),
                chunk.assignee_label,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    ("当前分工 · " if column == 0 and chunk.is_current else "")
                    + value
                )
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, chunk.chunk_id)
                    if chunk.is_current:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setForeground(Qt.GlobalColor.darkCyan)
                self.chunk_table.setItem(row, column, item)

    def _populate_controls(self):
        combos = (
            self.rename_chunk, self.move_source, self.move_destination,
            self.release_source, self.dissolve_chunk, self.assign_chunk,
            self.reassign_chunk, self.unassign_chunk,
        )
        previous = {combo: combo.currentData() for combo in combos}
        for combo in combos:
            combo.clear()
            for chunk in self._view.chunks:
                combo.addItem(chunk.name, chunk.chunk_id)
            index = combo.findData(previous[combo])
            if index >= 0:
                combo.setCurrentIndex(index)
        self.reorder_list.clear()
        for chunk in self._view.chunks:
            item = QListWidgetItem(chunk.name)
            item.setData(Qt.ItemDataRole.UserRole, chunk.chunk_id)
            self.reorder_list.addItem(item)

    def _configure_actions(self):
        view = self._view
        if view.mode is ChunkApplicationMode.NO_PLAN:
            actions = (("partition", "拆分项目"),)
        elif view.mode is ChunkApplicationMode.ACTIVE:
            actions = (("split_evenly", "拆分分工"), ("merge", "合并分工"))
        elif view.safe_code == "CHUNK.REBASE_REQUIRED":
            actions = (("rebase", "同步工作区变化"),)
        else:
            actions = ()
        self.action_combo.blockSignals(True)
        self.action_combo.clear()
        for key, label in actions:
            self.action_combo.addItem(label, key)
        self.action_combo.blockSignals(False)
        self.action_combo.setEnabled(len(actions) > 1)
        self.advanced_button.setVisible(view.mode is ChunkApplicationMode.ACTIVE)
        if view.mode is not ChunkApplicationMode.ACTIVE:
            self.advanced_button.setChecked(False)
        self.preview_button.setEnabled(bool(actions))
        self.select_all_chunks_button.setText(f"全选 {len(view.chunks)} 个分工")
        attached = sum(choice.attached for choice in self._segment_choices)
        self.partition_group_count.setMaximum(max(2, min(_MAX_GROUPS, attached)))
        self.partition_scope.setText(
            f"全项目可用段落 {attached}；按项目顺序平衡拆分。"
        )
        self._set_action(actions[0][0] if actions else "partition")

    def _select_default_chunk(self):
        if not self.chunk_table.rowCount():
            return
        row = 0
        for candidate in range(self.chunk_table.rowCount()):
            if self.chunk_table.item(candidate, 0).data(Qt.ItemDataRole.UserRole) == self._view.current_chunk_id:
                row = candidate
                break
        self.chunk_table.selectRow(row)

    def _primary_changed(self):
        if self._refreshing:
            return
        key = self.action_combo.currentData()
        if type(key) is str:
            self.advanced_button.setChecked(False)
            self._set_action(key)

    def _advanced_toggled(self, checked):
        self.advanced_combo.setVisible(checked)
        self.advanced_help.setVisible(checked)
        key = self.advanced_combo.currentData() if checked else self.action_combo.currentData()
        if type(key) is str:
            self._set_action(key)

    def _advanced_changed(self):
        if self.advanced_button.isChecked():
            key = self.advanced_combo.currentData()
            if type(key) is str:
                self._set_action(key)

    def _set_action(self, key):
        self._pending_segment_selection_request = None
        self._current_action = key
        self.operation_pages.setCurrentIndex(self._pages.get(key, 0))
        self._fit_operation_page()
        needs_segment_scope = key in {"create", "exact_split", "move", "release"}
        self.select_all_chunks_button.setVisible(key == "merge")
        self.segment_selection_panel.setVisible(needs_segment_scope)
        self._revalidate_selected_segments()
        self._scope_changed()
        self._invalidate_preview()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt virtual name
        """Make inclusive Shift range selection deterministic on every Qt platform."""

        if (
            watched is self.chunk_table.viewport()
            and self._current_action == "merge"
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            index = self.chunk_table.indexAt(event.position().toPoint())
            if index.isValid():
                row = index.row()
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    anchor = self._chunk_selection_anchor_row
                    if anchor is None:
                        current = self.chunk_table.currentRow()
                        anchor = row if current < 0 else current
                    first, last = sorted((anchor, row))
                    model = self.chunk_table.model()
                    selection = self.chunk_table.selectionModel()
                    selection.select(
                        QItemSelection(
                            model.index(first, 0),
                            model.index(last, self.chunk_table.columnCount() - 1),
                        ),
                        QItemSelectionModel.SelectionFlag.ClearAndSelect
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
                    selection.setCurrentIndex(
                        model.index(row, 0),
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
                    return True
                self._chunk_selection_anchor_row = row
        return super().eventFilter(watched, event)

    def _fit_operation_page(self):
        current = self.operation_pages.currentWidget()
        if current is None:
            return
        layout = current.layout()
        if layout is not None:
            layout.activate()
        wanted = max(
            current.sizeHint().height(),
            current.minimumSizeHint().height(),
        )
        self.operation_pages.setFixedHeight(max(40, wanted))
        self.operation_pages.updateGeometry()

    def _scope_changed(self):
        if self._refreshing or not hasattr(self, "chunk_table"):
            return
        mode = (QAbstractItemView.SelectionMode.ExtendedSelection
                if self._current_action == "merge"
                else QAbstractItemView.SelectionMode.SingleSelection)
        if self.chunk_table.selectionMode() != mode:
            self.chunk_table.setSelectionMode(mode)
        selected = self._selected_chunk_ids()
        if self._current_action in {"split_evenly", "exact_split"}:
            self.chunk_scope_hint.setText("选择 1 个原分工。")
        elif self._current_action == "merge":
            self.chunk_scope_hint.setText(
                f"已选 {len(selected)} / {len(self._view.chunks)}；"
                "可全选，或按住 Shift / Command 多选。"
            )
        else:
            self.chunk_scope_hint.setText("主要操作选择分工；高级操作可进一步选择段落范围。")
        if self._current_action == "split_evenly":
            self._update_split_context(selected)
        elif self._current_action == "exact_split":
            self._update_exact_assignment(selected)
            self._revalidate_selected_segments()
        elif self._current_action == "merge":
            names = [self._chunk(chunk_id).name for chunk_id in selected]
            self.merge_scope.setText("已选择：" + ("、".join(names) if names else "无"))
        self._fit_operation_page()
        self._invalidate_preview()

    def _select_all_chunks(self):
        if self._current_action != "merge":
            return
        self.chunk_table.selectAll()
        if self.chunk_table.rowCount():
            self._chunk_selection_anchor_row = 0

    def _update_split_context(self, selected):
        if len(selected) != 1:
            self.split_even_scope.setText("请在左侧选择一个原分工。")
            self.split_group_count.setMaximum(2)
            return
        source = self._chunk(selected[0])
        self.split_group_count.setMaximum(max(2, min(_MAX_GROUPS, source.member_count)))
        self.split_even_scope.setText(
            f"将“{source.name}”的 {source.member_count} 个段落按项目顺序平衡拆分。"
        )
        if source.assigned_to_current_reference:
            self.split_assignment.setEnabled(True)
            if self.split_assignment.currentData() not in {"inherit", "unassign"}:
                self.split_assignment.setCurrentIndex(0)
        else:
            self.split_assignment.setCurrentIndex(self.split_assignment.findData("unassign"))
            self.split_assignment.setEnabled(False)

    def _update_exact_assignment(self, selected):
        if len(selected) == 1 and not self._chunk(selected[0]).assigned_to_current_reference:
            self.exact_split_assignment.setCurrentIndex(self.exact_split_assignment.findData("unassign"))
            self.exact_split_assignment.setEnabled(False)
        else:
            self.exact_split_assignment.setEnabled(True)

    def _chunk(self, chunk_id):
        return next(chunk for chunk in self._view.chunks if chunk.chunk_id == chunk_id)

    def _selected_chunk_ids(self):
        model = self.chunk_table.selectionModel()
        if model is None:
            return ()
        live = {chunk.chunk_id for chunk in self._view.chunks}
        return tuple(
            self.chunk_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in sorted(model.selectedRows(), key=lambda item: item.row())
            if self.chunk_table.item(index.row(), 0) is not None
            and self.chunk_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) in live
        )

    def _selected_segments(self):
        return self._selected_segment_identities

    def _allowed_segment_choices(self, action=None):
        action = self._current_action if action is None else action
        source_chunk_id = None
        if action == "create":
            return tuple(
                choice
                for choice in self._segment_choices
                if choice.attached and choice.chunk_id is None
            )
        if action == "exact_split":
            selected = self._selected_chunk_ids()
            if len(selected) == 1:
                source_chunk_id = selected[0]
        elif action == "move":
            source_chunk_id = self.move_source.currentData()
        elif action == "release":
            source_chunk_id = self.release_source.currentData()
        if type(source_chunk_id) is not str or not source_chunk_id:
            return ()
        return tuple(
            choice
            for choice in self._segment_choices
            if choice.attached and choice.chunk_id == source_chunk_id
        )

    def _update_segment_selection_summary(self):
        count = len(self._selected_segment_identities)
        if count:
            self.selection_summary.setText(
                f"已选择 {count} 个段落；可返回浏览 / 校对页调整。"
            )
        else:
            self.selection_summary.setText(
                "尚未选择段落；将在浏览 / 校对页显示 source / target 上下文。"
            )
        self.split_cut.setMaximum(max(1, count - 1))

    def _revalidate_selected_segments(self):
        allowed = {
            choice.identity for choice in self._allowed_segment_choices()
        }
        self._selected_segment_identities = tuple(
            choice.identity
            for choice in self._segment_choices
            if choice.identity in allowed
            and choice.identity in self._selected_segment_identities
        )
        self._pending_segment_selection_request = None
        self._update_segment_selection_summary()

    def _segment_selection_context_changed(self, *_args):
        if self._refreshing:
            return
        self._revalidate_selected_segments()
        self._invalidate_preview()

    def _request_segment_selection(self):
        allowed_choices = self._allowed_segment_choices()
        if not allowed_choices:
            code = (
                "CHUNK.CHUNK_REQUIRED"
                if self._current_action == "exact_split"
                else "CHUNK.MEMBERS_REQUIRED"
            )
            self._show_error(code)
            return
        allowed = tuple(choice.identity for choice in allowed_choices)
        selected_set = set(self._selected_segment_identities)
        selected = tuple(identity for identity in allowed if identity in selected_set)
        bulk_label = None
        bulk_identities = ()
        if self._current_action == "create":
            bulk_label = f"选择全部尚未分工（{len(allowed)}）"
            bulk_identities = allowed
        minimum = 2 if self._current_action == "exact_split" else 1
        if len(allowed) < minimum:
            self._show_error("CHUNK.SPLIT_MEMBERS_REQUIRED")
            return
        request = ChunkApplicationSegmentSelectionRequest(
            action=self._current_action,
            action_label=_LABELS[self._current_action],
            allowed_identities=allowed,
            selected_identities=selected,
            bulk_select_label=bulk_label,
            bulk_select_identities=bulk_identities,
            minimum_selection=minimum,
        )
        request.__post_init__()
        self._pending_segment_selection_request = request
        self.segmentSelectionRequested.emit(request)
        self.segment_selection_requested.emit(request)

    def accept_segment_selection(self, request, identities):
        """Consume the exact one-use Browse/Review result in issued order."""

        if (
            type(request) is not ChunkApplicationSegmentSelectionRequest
            or request is not self._pending_segment_selection_request
        ):
            raise ValueError("CHUNK.SEGMENT_SELECTION_STALE")
        request.__post_init__()
        if type(identities) is not tuple or any(
            type(identity) is not SegmentIdentity for identity in identities
        ):
            raise TypeError("segment selection result must contain exact identities")
        allowed_order = {
            identity: index
            for index, identity in enumerate(request.allowed_identities)
        }
        if len(identities) != len(set(identities)) or any(
            identity not in allowed_order for identity in identities
        ):
            raise ValueError("CHUNK.SEGMENT_SELECTION_INVALID")
        if tuple(sorted(identities, key=allowed_order.__getitem__)) != identities:
            raise ValueError("CHUNK.SEGMENT_SELECTION_ORDER_INVALID")
        if identities and len(identities) < request.minimum_selection:
            raise ValueError("CHUNK.SEGMENT_SELECTION_TOO_SMALL")
        self._pending_segment_selection_request = None
        self._selected_segment_identities = identities
        self._update_segment_selection_summary()
        self._invalidate_preview()

    def cancel_segment_selection(self, request):
        if request is self._pending_segment_selection_request:
            self._pending_segment_selection_request = None

    def set_selected_segment_identities(self, identities):
        """Compatibility helper; primary split/merge never uses this input."""
        if type(identities) is not tuple or any(
            type(identity) is not SegmentIdentity for identity in identities
        ):
            raise TypeError("selected segment identities must be an exact tuple")
        known = {
            choice.identity
            for choice in self._segment_choices
            if choice.attached
        }
        wanted = set(identities)
        if len(wanted) != len(identities) or any(
            identity not in known for identity in identities
        ):
            raise ValueError("CHUNK.SEGMENT_SELECTION_INVALID")
        self._selected_segment_identities = tuple(
            choice.identity
            for choice in self._segment_choices
            if choice.identity in wanted
        )
        self._pending_segment_selection_request = None
        self._update_segment_selection_summary()
        self._invalidate_preview()

    def set_split_children(self, children):
        if type(children) is not tuple or any(type(child) is not ChunkApplicationSplitChild for child in children):
            raise TypeError("split children must be frozen application contracts")
        for child in children:
            child.__post_init__()
        self._split_children = children
        total = sum(len(child.members) for child in children)
        self.split_summary.setText(f"已提供 {len(children)} 个子分工，共 {total} 个成员。")
        self._invalidate_preview()

    @staticmethod
    def _sync_names(widget, count):
        while widget.count() < count:
            item = QListWidgetItem(f"分工 {widget.count() + 1}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            widget.addItem(item)
        while widget.count() > count:
            widget.takeItem(widget.count() - 1)

    @staticmethod
    def _names(widget):
        names = tuple(widget.item(index).text().strip() for index in range(widget.count()))
        if any(not name for name in names):
            raise ValueError("CHUNK.NAME_REQUIRED")
        return names

    def _invalidate_preview(self, *_args):
        if self._refreshing:
            return
        had_preview = self._preview is not None
        self._preview = None
        self.confirm_check.setChecked(False)
        self.confirm_check.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.preview_panel.setProperty("state", "idle")
        if had_preview:
            self.preview_panel.setText(
                "<div class='preview'><b>设置已变化</b><br>"
                "请重新生成预览后再确认发布。</div>"
            )
            self.preview_button.setText("重新生成预览")
        self.preview_panel.style().unpolish(self.preview_panel)
        self.preview_panel.style().polish(self.preview_panel)

    @staticmethod
    def _safe_code(error):
        code = getattr(error, "code", None)
        if type(code) is str and code:
            return code
        if error.args and type(error.args[0]) is str:
            candidate = error.args[0]
            if candidate and candidate.upper() == candidate and " " not in candidate:
                return candidate
        return type(error).__name__

    @staticmethod
    def _required(value, code):
        if value is None or (type(value) is str and not value.strip()):
            raise ValueError(code)
        return value.strip() if type(value) is str else value

    def _preview_selected_action(self):
        self._invalidate_preview()
        try:
            preview = self._route(self._current_action)
            if type(preview) is not ChunkApplicationMutationPreview:
                raise TypeError("CHUNK.PREVIEW_CONTRACT_INVALID")
            preview.__post_init__()
        except Exception as error:
            self._show_error(self._safe_code(error))
            return
        self._preview = preview
        self._render_preview(preview)
        self.preview_button.setText("生成预览")
        self.confirm_check.setEnabled(not preview.blockers)
        self._update_apply_enabled()

    def _one_chunk(self):
        selected = self._selected_chunk_ids()
        if len(selected) != 1:
            raise ValueError("CHUNK.CHUNK_REQUIRED")
        return selected[0]

    def _decision(self, combo, source_id):
        if not self._chunk(source_id).assigned_to_current_reference:
            return "unassign"
        return self._required(combo.currentData(), "CHUNK.ASSIGNMENT_DECISION_REQUIRED")

    def _route(self, action):
        segments = self._selected_segments()
        if action == "partition":
            return self._facade.preview_partition_project(self._names(self.partition_group_names))
        if action == "split_evenly":
            source = self._one_chunk()
            return self._facade.preview_split_chunk_evenly(
                source, self._names(self.split_group_names), self._decision(self.split_assignment, source)
            )
        if action == "merge":
            sources = self._selected_chunk_ids()
            if len(sources) < 2:
                raise ValueError("CHUNK.MERGE_SOURCES_REQUIRED")
            result_name = self.merge_name.text().strip() or None
            return self._facade.preview_merge_chunks(
                sources, result_name,
                assign_to_current_reference=self.merge_assignment.currentData(),
            )
        if action == "create":
            if not segments:
                raise ValueError("CHUNK.MEMBERS_REQUIRED")
            return self._facade.preview_create_chunk(self._required(self.create_name.text(), "CHUNK.NAME_REQUIRED"), segments)
        if action == "rename":
            return self._facade.preview_rename_chunk(
                self._required(self.rename_chunk.currentData(), "CHUNK.CHUNK_REQUIRED"),
                self._required(self.rename_name.text(), "CHUNK.NAME_REQUIRED"),
            )
        if action == "reorder":
            order = tuple(self.reorder_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.reorder_list.count()))
            return self._facade.preview_reorder_chunks(order)
        if action == "exact_split":
            source = self._one_chunk()
            decision = self._decision(self.exact_split_assignment, source)
            assign = self._chunk(source).assigned_to_current_reference and decision == "inherit"
            children = self._split_children
            if not children:
                if len(segments) < 2:
                    raise ValueError("CHUNK.SPLIT_MEMBERS_REQUIRED")
                cut = self.split_cut.value()
                if cut <= 0 or cut >= len(segments):
                    raise ValueError("CHUNK.SPLIT_PARTITION_INVALID")
                children = (
                    ChunkApplicationSplitChild(self._required(self.split_first_name.text(), "CHUNK.NAME_REQUIRED"), segments[:cut], assign),
                    ChunkApplicationSplitChild(self._required(self.split_second_name.text(), "CHUNK.NAME_REQUIRED"), segments[cut:], assign),
                )
            else:
                children = tuple(
                    ChunkApplicationSplitChild(child.name, child.members, assign if child.assign_to_current_reference is None else child.assign_to_current_reference)
                    for child in children
                )
            return self._facade.preview_split_chunk(source, children)
        if action == "move":
            if not segments:
                raise ValueError("CHUNK.MEMBERS_REQUIRED")
            return self._facade.preview_move_members(
                self._required(self.move_source.currentData(), "CHUNK.CHUNK_REQUIRED"),
                self._required(self.move_destination.currentData(), "CHUNK.CHUNK_REQUIRED"),
                segments, retire_source_if_empty=self.move_retire.isChecked(),
            )
        if action == "release":
            if not segments:
                raise ValueError("CHUNK.MEMBERS_REQUIRED")
            return self._facade.preview_release_members(
                self._required(self.release_source.currentData(), "CHUNK.CHUNK_REQUIRED"),
                segments, retire_source_if_empty=self.release_retire.isChecked(),
            )
        if action == "dissolve_chunk":
            return self._facade.preview_dissolve_chunk(self._required(self.dissolve_chunk.currentData(), "CHUNK.CHUNK_REQUIRED"))
        if action == "dissolve_plan":
            return self._facade.preview_dissolve_plan()
        if action == "assign":
            return self._facade.preview_assign_to_current_reference(self._required(self.assign_chunk.currentData(), "CHUNK.CHUNK_REQUIRED"))
        if action == "reassign":
            return self._facade.preview_reassign_to_current_reference(self._required(self.reassign_chunk.currentData(), "CHUNK.CHUNK_REQUIRED"))
        if action == "unassign":
            return self._facade.preview_unassign_chunk(self._required(self.unassign_chunk.currentData(), "CHUNK.CHUNK_REQUIRED"))
        if action == "rebase":
            inspection = self._rebase_inspection
            if inspection is None:
                raise ValueError("CHUNK.REBASE_INSPECTION_REQUIRED")
            missing_decision = self.rebase_missing_decision.currentData()
            empty_decision = self.rebase_empty_decision.currentData()
            if inspection.missing_members and missing_decision != "release":
                raise ValueError("CHUNK.REBASE_DECISION_REQUIRED")
            if inspection.empty_chunk_ids and empty_decision is None:
                raise ValueError("CHUNK.REBASE_DECISION_REQUIRED")
            if empty_decision == "dissolve":
                if not inspection.all_chunks_empty:
                    raise ValueError("CHUNK.REBASE_DECISION_INVALID")
                return self._facade.preview_dissolve_plan()
            return self._facade.preview_workspace_rebase(
                inspection.missing_members if missing_decision == "release" else (),
                inspection.empty_chunk_ids if empty_decision == "retire" else (),
            )
        if action == "undo":
            return self._facade.preview_undo_current_head()
        raise ValueError("CHUNK.ACTION_INVALID")

    @staticmethod
    def _list_html(values, empty):
        if not values:
            return f"<span class='quiet'>{escape(empty)}</span>"
        return "<br>".join(f"• {escape(value)}" for value in values)

    def _render_preview(self, preview):
        classification = preview.classification or "常规发布"
        truncated = " · 计数已截断" if preview.truncated else ""
        self.preview_panel.setText(
            "<div class='preview'>"
            f"<h3>{escape(_LABELS.get(self._current_action, self._current_action))}</h3>"
            f"<p><b>分类</b>　{escape(classification)}{truncated}</p>"
            f"<p><b>影响</b>　分工 {preview.affected_chunk_count} · "
            f"成员 {preview.affected_member_count} · 分配 {preview.assignment_count}</p>"
            f"<p><b>变化</b>　新建 {preview.created_chunk_count} · "
            f"退役 {preview.retired_chunk_count} · 缺失 {preview.missing_member_count} · "
            f"新未分配 {preview.new_unallocated_count}</p>"
            f"<p><b>警告</b><br>{self._list_html(preview.warnings, '无')}</p>"
            f"<p><b>阻断</b><br>{self._list_html(preview.blockers, '无')}</p>"
            "</div>"
        )
        self.preview_panel.setProperty("state", "blocked" if preview.blockers else "ready")
        self.preview_panel.style().unpolish(self.preview_panel)
        self.preview_panel.style().polish(self.preview_panel)

    def _show_error(self, code):
        self._preview = None
        self.confirm_check.setChecked(False)
        self.confirm_check.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.preview_panel.setText(
            "<div class='error'><b>操作未发布</b><br>"
            f"<code>{escape(code)}</code></div>"
        )
        self.preview_panel.setProperty("state", "error")
        self.preview_panel.style().unpolish(self.preview_panel)
        self.preview_panel.style().polish(self.preview_panel)

    def _update_apply_enabled(self, *_args):
        self.apply_button.setEnabled(
            self._preview is not None
            and not self._preview.blockers
            and self.confirm_check.isChecked()
        )

    def _apply_preview(self):
        preview = self._preview
        if preview is None or preview.blockers or not self.confirm_check.isChecked():
            return
        published_label = _LABELS.get(self._current_action, self._current_action)
        self._preview = None
        self.confirm_check.setChecked(False)
        self.confirm_check.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.preview_button.setEnabled(False)
        try:
            receipt = self._facade.apply_mutation(preview)
        except Exception as error:
            self.preview_button.setEnabled(
                self._view.mode is not ChunkApplicationMode.BLOCKED
                or self._view.safe_code == "CHUNK.REBASE_REQUIRED"
            )
            self._show_error(self._safe_code(error))
            return
        self.mutationCommitted.emit(receipt)
        self.mutation_committed.emit(receipt)
        updated = None
        provider = getattr(self._facade, "project_view", None)
        if callable(provider):
            try:
                candidate = provider()
                if type(candidate) is ChunkApplicationProjectView:
                    updated = candidate
            except Exception:
                pass
        if updated is not None:
            self.refresh(updated)
            self.viewChanged.emit(updated)
            self.view_changed.emit(updated)
            self.preview_panel.setText(
                "<div class='success'><b>已发布</b><br>"
                f"操作：{escape(published_label)}</div>"
            )
            self.preview_panel.setProperty("state", "success")
        else:
            self.preview_button.setEnabled(True)
        self.viewRefreshRequested.emit()
        self.view_refresh_requested.emit()


_STYLE = """
QDialog#chunkManagerDialog { background: #edf5fb; color: #143c58; }
QLabel#chunkManagerTitle { color: #073b63; font-size: 25px; font-weight: 800; }
QLabel#chunkManagerSubtitle, QLabel#chunkProjectSummary,
QLabel#chunkSelectionSummary, QLabel#chunkFormHint { color: #5b7890; }
QLabel#chunkModeBadge {
    background: transparent; border: 0;
    color: #007e9f; font-weight: 700; padding: 0;
}
QLabel#chunkModeBadge[mode="blocked"] {
    color: #92510b;
}
QFrame#chunkCard { background: #ffffff; border: 1px solid #c7ddeb; border-radius: 14px; }
QFrame#chunkSegmentSelectionPanel {
    background: #eef7fb; border: 1px solid #c4dfeb; border-radius: 9px;
}
QLabel#chunkSectionTitle { color: #0b405f; font-size: 16px; font-weight: 800; }
QLabel#chunkSubsectionTitle { color: #365a72; font-weight: 700; }
QTableWidget, QListWidget, QLineEdit, QComboBox, QSpinBox {
    background: #fbfdff; border: 1px solid #c5d8e6; border-radius: 7px;
    padding: 5px; selection-background-color: #d9f1f8; selection-color: #073b63;
}
QHeaderView::section {
    background: #e6f0f7; border: 0; border-bottom: 1px solid #c5d8e6;
    color: #365a72; font-weight: 700; padding: 7px;
}
QSplitter::handle { background: #dbe9f2; width: 3px; height: 3px; }
QScrollArea#chunkPreviewScroll {
    border: 1px solid #c5d8e6; border-radius: 9px; background: #f8fbfd;
}
QLabel#chunkPreviewPanel { background: #f8fbfd; color: #244b63; padding: 12px; }
QLabel#chunkPreviewPanel[state="blocked"], QLabel#chunkPreviewPanel[state="error"] {
    background: #fff4e5; color: #7b420b;
}
QLabel#chunkPreviewPanel[state="ready"], QLabel#chunkPreviewPanel[state="success"] {
    background: #e7f7ef; color: #12633e;
}
QPushButton {
    background: #ffffff; border: 1px solid #a9c5d7; border-radius: 8px;
    color: #164762; font-weight: 700; min-height: 30px; padding: 2px 13px;
}
QPushButton#chunkApplyButton, QPushButton#chunkPreviewButton {
    background: #08a5c8; border-color: #08a5c8; color: #ffffff;
}
QPushButton#chunkAdvancedButton:checked { background: #e2f4fa; border-color: #74cce0; }
QPushButton:disabled { background: #e8eef2; border-color: #d5e0e7; color: #8aa0af; }
"""

__all__ = ["QtChunkManagerDialog"]
