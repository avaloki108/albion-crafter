from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    """Typed origin of a value used by the application."""

    AODP_LIVE = "aodp_live"
    AODP_CACHED = "aodp_cached"
    STATIC_GAME_DATA = "static_game_data"
    USER_OVERRIDE = "user_override"
    USER_PROFILE = "user_profile"
    DERIVED_MECHANICS = "derived_mechanics"
    TEST_FIXTURE = "test_fixture"
    DEMO_SAMPLE = "demo_sample"
    UNKNOWN = "unknown"

    @property
    def is_production_market_data(self) -> bool:
        return self in {Provenance.AODP_LIVE, Provenance.AODP_CACHED}

    @property
    def is_actionable_price_source(self) -> bool:
        return self in {
            Provenance.AODP_LIVE,
            Provenance.AODP_CACHED,
            Provenance.USER_OVERRIDE,
        }
