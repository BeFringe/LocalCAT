"""Shared LocalCAT Qt popup and menu presentation contracts."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QMenu, QStyledItemDelegate

from qt_theme import system_uses_dark_theme


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


LOCALCAT_DARK_COMBO_POPUP_STYLE = """
QAbstractItemView {
    color: #e7edf3;
    background-color: #20242a;
    selection-color: #f5fbff;
    selection-background-color: #294f60;
    border: 1px solid #48515b;
    outline: 0;
}
QAbstractItemView::item {
    color: #e7edf3;
    background-color: #20242a;
    min-height: 30px;
    padding: 2px 8px;
}
QAbstractItemView::item:hover {
    color: #f5fbff;
    background-color: #33414b;
}
QAbstractItemView::item:selected {
    color: #f5fbff;
    background-color: #294f60;
}
"""


LOCALCAT_DARK_MENU_STYLE = """
QMenu {
    color: #e7edf3;
    background-color: #20242a;
    border: 1px solid #48515b;
    padding: 4px;
    font-size: 13px;
}
QMenu::item {
    color: #e7edf3;
    background-color: #20242a;
    min-height: 28px;
    padding: 3px 24px 3px 10px;
    border: 1px solid transparent;
    border-radius: 4px;
}
QMenu::item:selected {
    color: #f5fbff;
    background-color: #294f60;
    border-color: #4c8aa3;
}
QMenu::item:disabled {
    color: #7f8993;
    background-color: #20242a;
}
QMenu::separator {
    height: 1px;
    background: #3b424a;
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
    apply_combo_popup_theme(combo)
    popup.setItemDelegate(QStyledItemDelegate(popup))


def apply_combo_popup_theme(combo: QComboBox) -> None:
    """Refresh an existing combo popup after the system theme changes."""

    popup = combo.view()
    popup.setStyleSheet(
        LOCALCAT_DARK_COMBO_POPUP_STYLE
        if system_uses_dark_theme(combo)
        else LOCALCAT_COMBO_POPUP_STYLE
    )


def configure_menu(menu: QMenu) -> None:
    """Apply the one LocalCAT action-menu contract."""

    apply_menu_theme(menu)


def apply_menu_theme(menu: QMenu) -> None:
    """Refresh an existing menu after the system theme changes."""

    menu.setStyleSheet(
        LOCALCAT_DARK_MENU_STYLE
        if system_uses_dark_theme(menu)
        else LOCALCAT_MENU_STYLE
    )
