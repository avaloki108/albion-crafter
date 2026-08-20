from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from albion_crafter.market.recipe_refresh import (
    RecipePriceRefreshRequest,
    RecipePriceRefreshService,
)


class CalculatorRefreshSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class CalculatorRefreshWorker(QRunnable):
    """Run one explicit calculator price refresh away from the GUI thread."""

    def __init__(
        self,
        service: RecipePriceRefreshService,
        request: RecipePriceRefreshRequest,
    ) -> None:
        super().__init__()
        self.service = service
        self.request = request
        self.signals = CalculatorRefreshSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        """Ask the bounded refresh to stop before its next request boundary."""

        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = self.service.refresh(
                self.request,
                is_cancelled=self._cancelled.is_set,
                on_progress=self.signals.progress.emit,
            )
        except Exception as exc:  # worker boundary: the GUI must make failures visible
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)
