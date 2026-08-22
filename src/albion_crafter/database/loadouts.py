from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from albion_crafter.core.loadouts import SavedLoadout

from .database import SettingsRepository

SAVED_LOADOUTS_SETTING = "saved_loadouts_v1"


class SavedLoadoutRepository:
    """Persist versioned loadout documents without changing the market schema."""

    def __init__(self, settings: SettingsRepository) -> None:
        self.settings = settings

    def list(self) -> tuple[SavedLoadout, ...]:
        raw = self.settings.get(SAVED_LOADOUTS_SETTING, {"version": 1, "loadouts": []})
        if not isinstance(raw, dict) or raw.get("version") != 1:
            return ()
        records = raw.get("loadouts", [])
        if not isinstance(records, list):
            return ()
        loadouts: list[SavedLoadout] = []
        for value in records:
            try:
                loadouts.append(SavedLoadout.from_dict(value))
            except (TypeError, ValueError, OverflowError):
                # One damaged user document must not hide otherwise healthy builds.
                continue
        return tuple(sorted(loadouts, key=lambda value: value.updated_at, reverse=True))

    def get(self, loadout_id: str) -> SavedLoadout | None:
        return next((value for value in self.list() if value.id == loadout_id), None)

    def save(self, loadout: SavedLoadout) -> SavedLoadout:
        saved = replace(loadout, updated_at=datetime.now(UTC))
        by_id = {value.id: value for value in self.list()}
        by_id[saved.id] = saved
        self._write(tuple(by_id.values()))
        return saved

    def delete(self, loadout_id: str) -> bool:
        current = self.list()
        retained = tuple(value for value in current if value.id != loadout_id)
        if len(retained) == len(current):
            return False
        self._write(retained)
        return True

    def _write(self, loadouts: tuple[SavedLoadout, ...]) -> None:
        ordered = sorted(loadouts, key=lambda value: value.updated_at, reverse=True)
        self.settings.set(
            SAVED_LOADOUTS_SETTING,
            {"version": 1, "loadouts": [value.to_dict() for value in ordered]},
        )
