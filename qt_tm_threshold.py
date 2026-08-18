"""Shared Qt presentation helpers for the Controller-owned fuzzy threshold."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QInputDialog, QPushButton, QWidget

from editor_contracts import (
    RetrievalDisplayState,
    TMPreferences,
    TMThresholdUpdateOutcome,
)


class TMThresholdButton(QPushButton):
    """Make both standard activation keys explicit on every supported Qt host."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            if self.property("fuzzyAvailable") is True:
                self.click()
            return
        super().keyPressEvent(event)


def _threshold_text(preferences: TMPreferences) -> str:
    preferences.__post_init__()
    percentage = preferences.minimum_similarity * 100.0
    if percentage.is_integer():
        rendered = str(int(percentage))
    else:
        rendered = f"{percentage:.6f}".rstrip("0").rstrip(".")
    return f"Fuzzy 阈值 {rendered}%"


def _fuzzy_disabled_reason(status: RetrievalDisplayState) -> str:
    status.__post_init__()
    fuzzy_codes = tuple(code for code in status.safe_codes if "FUZZY" in code)
    code = fuzzy_codes[0] if fuzzy_codes else None
    if code is None:
        return "Fuzzy 检索能力尚不可用"
    if "BENCHMARK" in code:
        if "EXPIRED" in code:
            return "Fuzzy 性能验证已过期"
        return "Fuzzy 性能验证尚未通过"
    if "CORRECTNESS" in code:
        if "EXPIRED" in code:
            return "Fuzzy 正确性验证已过期"
        return "Fuzzy 正确性验证尚未通过"
    return "Fuzzy 检索能力尚不可用"


def configure_tm_threshold_entry(
    button: QPushButton,
    state_label: QLabel,
    *,
    preferences: TMPreferences,
    retrieval_status: RetrievalDisplayState,
) -> None:
    """Render one entry from defensive Controller values without retaining state."""

    if not isinstance(button, QPushButton) or type(state_label) is not QLabel:
        raise TypeError("TM threshold entry requires exact Qt widgets")
    preferences.__post_init__()
    retrieval_status.__post_init__()
    value_text = _threshold_text(preferences)
    if retrieval_status.fuzzy_available:
        state_text = "Fuzzy 可用"
        tooltip = "调整本机共享的 Fuzzy 最低相似度（60%～100%）"
    else:
        reason = _fuzzy_disabled_reason(retrieval_status)
        state_text = f"Fuzzy 不可用：{reason}"
        tooltip = state_text
    button.setText(value_text)
    button.setEnabled(True)
    button.setToolTip(tooltip)
    button.setAccessibleName(f"{value_text}，{state_text}")
    button.setProperty("fuzzyAvailable", retrieval_status.fuzzy_available)
    state_label.setText(state_text)
    state_label.setToolTip(tooltip)
    state_label.setAccessibleName(state_text)
    state_label.setProperty("fuzzyAvailable", retrieval_status.fuzzy_available)
    for widget in (button, state_label):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def prompt_tm_threshold(
    parent: QWidget,
    preferences: TMPreferences,
) -> float | None:
    """Ask for one constrained percentage; cancellation has no side effect."""

    preferences.__post_init__()
    percentage, accepted = QInputDialog.getDouble(
        parent,
        "调整 Fuzzy 阈值",
        "最低相似度（60%～100%）",
        preferences.minimum_similarity * 100.0,
        60.0,
        100.0,
        2,
        step=1.0,
    )
    if not accepted:
        return None
    return float(percentage / 100.0)


def tm_threshold_feedback(outcome: TMThresholdUpdateOutcome) -> str:
    """Map one body-free Controller outcome to finite non-blocking copy."""

    outcome.__post_init__()
    current = _threshold_text(outcome.preferences)
    if outcome.succeeded:
        return f"{current} 已保存；当前段建议已刷新。"
    if outcome.safe_code == "TM.THRESHOLD.PERSISTENCE_FAILED":
        return f"Fuzzy 阈值保存失败；仍使用 {current}。"
    if outcome.safe_code == "TM.THRESHOLD.INVALID":
        return f"Fuzzy 阈值必须在 60% 至 100% 之间；仍使用 {current}。"
    return f"Fuzzy 阈值未更新；仍使用 {current}。"


__all__ = [
    "TMThresholdButton",
    "configure_tm_threshold_entry",
    "prompt_tm_threshold",
    "tm_threshold_feedback",
]
