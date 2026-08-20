from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from albion_crafter.planning.preflight import FindMoneyPreflight
from albion_crafter.planning.service import FindMoneyRunResult, FindMoneyService


class PlanningCancellationToken:
    """Small thread-safe cancellation boundary shared by Qt and the planner."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class FindMoneyWorker(QObject):
    """Run the explicit network/planning stage on a dedicated ``QThread``."""

    progress = Signal(object)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        service: FindMoneyService,
        preflight: FindMoneyPreflight,
        *,
        refresh_current: bool = True,
        refresh_history: bool = True,
        cancellation: PlanningCancellationToken | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.preflight = preflight
        self.refresh_current = refresh_current
        self.refresh_history = refresh_history
        self.cancellation = cancellation or PlanningCancellationToken()

    @Slot()
    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        try:
            result: FindMoneyRunResult = self.service.execute(
                self.preflight,
                refresh_current=self.refresh_current,
                refresh_history=self.refresh_history,
                cancelled=self.cancellation.is_cancelled,
                progress=self.progress.emit,
            )
        except Exception as error:  # Qt boundary reports failures on the GUI thread.
            self.error.emit(f"{type(error).__name__}: {error}")
            return
        self.finished.emit(result)
