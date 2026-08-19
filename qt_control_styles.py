"""Shared LocalCAT Qt popup and menu presentation contracts."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QMenu, QStyledItemDelegate


LOCALCAT_COMBO_POPUP_STYLE = """
QAbstractItemView {
    color: #1f3850;
    background-color: #ffffff;
    selection-color: #0b304c;
    selection-background-color: #c4e8f2;
    border: 1px solid #9fb5c8;
    outline: 0;
}
QAbstractItemView::item {
    color: #1f3850;
    background-color: #ffffff;
    min-height: 30px;
    padding: 2px 8px;
}
QAbstractItemView::item:hover {
    color: #16344e;
    background-color: #e7f4f8;
}
QAbstractItemView::item:selected {
    color: #0b304c;
    background-color: #c4e8f2;
}
"""


LOCALCAT_MENU_STYLE = """
QMenu {
    color: #1f3850;
    background-color: #ffffff;
    border: 1px solid #9fb5c8;
    padding: 4px;
    font-size: 13px;
}
QMenu::item {
    color: #1f3850;
    background-color: #ffffff;
    min-height: 28px;
    padding: 3px 24px 3px 10px;
    border: 1px solid transparent;
    border-radius: 4px;
}
QMenu::item:selected {
    color: #0b304c;
    background-color: #c4e8f2;
    border-color: #75bfd3;
}
QMenu::item:disabled {
    color: #8a9aaa;
    background-color: #f6f8fa;
}
QMenu::separator {
    height: 1px;
    background: #d7e1e9;
    margin: 4px 7px;
}
"""


def configure_combo_popup(
    combo: QComboBox,
    *,
    object_name: str,
    accessible_name: str,
) -> None:
    """Apply the one LocalCAT popup contract without changing combo data."""

    popup = combo.view()
    popup.setObjectName(object_name)
    popup.setAccessibleName(accessible_name)
    popup.setStyleSheet(LOCALCAT_COMBO_POPUP_STYLE)
    popup.setItemDelegate(QStyledItemDelegate(popup))


def configure_menu(menu: QMenu) -> None:
    """Apply the one LocalCAT action-menu contract."""

    menu.setStyleSheet(LOCALCAT_MENU_STYLE)
