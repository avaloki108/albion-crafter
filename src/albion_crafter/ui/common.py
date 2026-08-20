from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem

from albion_crafter.core.freshness import future_offset_beyond_tolerance


def money(value: float | None) -> str:
    return "Missing" if value is None else f"{value:,.0f}"


def percent(value: float | None) -> str:
    return "Missing" if value is None else f"{value * 100:,.1f}%"


def age_text(timestamp: datetime | None, *, now: datetime | None = None) -> str:
    if timestamp is None:
        return "Unknown"
    current = now or datetime.now(UTC)
    excessive_future = future_offset_beyond_tolerance(timestamp, now=current)
    if excessive_future is not None:
        return f"Future-dated by {_duration_text(excessive_future.total_seconds())} (invalid)"
    seconds = max((current - timestamp).total_seconds(), 0)
    return _duration_text(seconds)


def _duration_text(seconds: float) -> str:
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


class SortableItem(QTableWidgetItem):
    def __init__(self, display: str, sort_value: float | str | None = None) -> None:
        super().__init__(display)
        self.sort_value = sort_value
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, SortableItem):
            left = self.sort_value
            right = other.sort_value
            if left is None:
                return right is not None or self.text().lower() < other.text().lower()
            if right is None:
                return False
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            return str(left).lower() < str(right).lower()
        return self.text().lower() < other.text().lower()
