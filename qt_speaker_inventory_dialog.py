"""Read-only raw speaker inventory dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from editor_controller import EditorController
from qt_speaker_avatar import SpeakerAvatarCatalog


class QtSpeakerInventoryDialog(QDialog):
    """Project-local speaker counts with optional bundled presentation assets."""

    def __init__(
        self,
        controller: EditorController,
        parent=None,
        *,
        avatar_catalog: SpeakerAvatarCatalog | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("speakerInventoryDialog")
        self.setWindowTitle("Raw speaker 盘点")
        self.setMinimumSize(560, 420)
        self.setAccessibleName("Raw speaker 盘点")
        self._avatar_catalog = avatar_catalog or SpeakerAvatarCatalog()

        inventory = controller.speaker_inventory()
        layout = QVBoxLayout(self)

        self.summary_label = QLabel(
            f"共 {inventory.segment_count} 段；"
            f"{len(inventory.items)} 个非空 raw speaker；"
            f"{inventory.empty_count} 段无 speaker。"
        )
        self.summary_label.setObjectName("speakerInventorySummary")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setAccessibleName(self.summary_label.text())
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(len(inventory.items), 4, self)
        self.table.setObjectName("speakerInventoryTable")
        self.table.setAccessibleName("Raw speaker 盘点表")
        self.table.setHorizontalHeaderLabels(
            ("头像", "RAW SPEAKER", "出现次数", "首次出现")
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        count_header_width = max(
            112,
            header.fontMetrics().horizontalAdvance("出现次数") + 40,
        )
        self.table.setColumnWidth(2, count_header_width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        for row, item in enumerate(inventory.items):
            avatar_label = QLabel(self.table)
            avatar_label.setObjectName(f"speakerInventoryAvatar{row}")
            avatar_label.setFixedSize(56, 56)
            avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar = self._avatar_catalog.avatar_pixmap(item.raw_speaker, 48)
            if avatar is None:
                avatar_label.setText("—")
                avatar_label.setToolTip("无内置头像")
                avatar_label.setAccessibleName(
                    f"{item.raw_speaker}：无内置头像"
                )
            else:
                avatar_label.setPixmap(avatar)
                avatar_label.setToolTip("内置 speaker 头像")
                avatar_label.setAccessibleName(
                    f"{item.raw_speaker}：内置 speaker 头像"
                )
            self.table.setCellWidget(row, 0, avatar_label)
            self.table.setItem(row, 1, self._readonly_item(item.raw_speaker))
            self.table.setItem(row, 2, self._readonly_item(str(item.count)))
            self.table.setItem(
                row,
                3,
                self._readonly_item(
                    f"第 {item.first_index + 1} 段 ({item.first_segment_id})"
                ),
            )
            self.table.setRowHeight(row, 60)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel(
            "当前项目没有非空 raw speaker。" if not inventory.items else ""
        )
        self.empty_label.setObjectName("speakerInventoryEmptyState")
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setVisible(not inventory.items)
        self.empty_label.setAccessibleName(
            self.empty_label.text() or "Raw speaker inventory 非空"
        )
        layout.addWidget(self.empty_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.setObjectName("speakerInventoryButtons")
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item
