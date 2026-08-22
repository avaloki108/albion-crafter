from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from albion_crafter.market.loadouts import (
    LoadoutPriceRefreshRequest,
    LoadoutPriceRefreshService,
)


class LoadoutRefreshSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class LoadoutRefreshWorker(QRunnable):
    def __init__(
        self,
        service: LoadoutPriceRefreshService,
        request: LoadoutPriceRefreshRequest,
    ) -> None:
        super().__init__()
        self.service = service
        self.request = request
        self.signals = LoadoutRefreshSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = self.service.refresh(
                self.request,
                is_cancelled=self._cancelled.is_set,
                on_progress=self.signals.progress.emit,
            )
        except Exception as exc:  # worker boundary keeps failures visible in the GUI
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit(result)
