"""Application-owned QMessageBox helpers with explicit Chinese button copy."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def ask_localized_question(
    parent: QWidget | None,
    *,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default_button: QMessageBox.StandardButton,
    button_labels: dict[QMessageBox.StandardButton, str],
) -> QMessageBox.StandardButton:
    """Ask one question without inheriting platform-language button labels."""

    prompt = QMessageBox(parent)
    prompt.setIcon(QMessageBox.Icon.Question)
    prompt.setWindowTitle(title)
    prompt.setText(text)
    prompt.setStandardButtons(buttons)
    prompt.setDefaultButton(default_button)
    for standard_button, label in button_labels.items():
        button = prompt.button(standard_button)
        if button is not None:
            button.setText(label)
    return QMessageBox.StandardButton(prompt.exec())


def show_localized_critical(
    parent: QWidget | None,
    *,
    title: str,
    text: str,
) -> None:
    """Show one critical message with an application-owned confirmation label."""

    prompt = QMessageBox(parent)
    prompt.setIcon(QMessageBox.Icon.Critical)
    prompt.setWindowTitle(title)
    prompt.setText(text)
    prompt.setStandardButtons(QMessageBox.StandardButton.Ok)
    prompt.setDefaultButton(QMessageBox.StandardButton.Ok)
    button = prompt.button(QMessageBox.StandardButton.Ok)
    if button is not None:
        button.setText("确定")
    prompt.exec()


__all__ = ["ask_localized_question", "show_localized_critical"]
