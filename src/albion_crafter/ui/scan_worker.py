from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from albion_crafter.opportunity.models import CancellationToken, ScanConstraints
from albion_crafter.opportunity.service import OpportunityScannerService


class ScanWorkerSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    error = Signal(str)


class OpportunityScanWorker(QRunnable):
    """Qt adapter; all opportunity-domain work remains outside the GUI module."""

    def __init__(
        self,
        service: OpportunityScannerService,
        constraints: ScanConstraints,
    ) -> None:
        super().__init__()
        self.service = service
        self.constraints = constraints
        self.cancellation = CancellationToken()
        self.signals = ScanWorkerSignals()

    def cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        try:
            result = self.service.scan(
                self.constraints,
                progress=self.signals.progress.emit,
                cancellation=self.cancellation,
            )
        except Exception as error:  # Qt boundary must deliver failures to the main thread.
            self.signals.error.emit(f"{type(error).__name__}: {error}")
            return
        self.signals.finished.emit(result)
