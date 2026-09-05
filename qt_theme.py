"""Small system-theme bridge shared by LocalCAT Qt surfaces."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QWidget


def color_scheme_uses_dark(value: object) -> bool | None:
    """Decode a concrete Qt color-scheme signal without rereading stale hints."""

    if isinstance(value, Qt.ColorScheme):
        if value == Qt.ColorScheme.Dark:
            return True
        if value == Qt.ColorScheme.Light:
            return False
    return None


def system_uses_dark_theme(widget: QWidget | None = None) -> bool:
    """Return the current Qt/OS dark-theme preference with a palette fallback."""

    application = QApplication.instance()
    if isinstance(application, QApplication):
        color_scheme = application.styleHints().colorScheme()
        if color_scheme == Qt.ColorScheme.Dark:
            return True
        if color_scheme == Qt.ColorScheme.Light:
            return False
        palette = application.palette()
    elif widget is not None:
        palette = widget.palette()
    else:
        return False
    return palette.color(QPalette.ColorRole.Window).lightnessF() < 0.5
