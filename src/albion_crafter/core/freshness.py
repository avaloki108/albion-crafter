from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class Freshness(StrEnum):
    """Age classification shared by market and user-observed evidence."""

    FRESH = "Fresh"
    AGING = "Aging"
    STALE = "Stale"
    FUTURE = "Future"
    UNKNOWN = "Unknown"


DEFAULT_CLOCK_SKEW_TOLERANCE = timedelta(minutes=2)


def future_offset_beyond_tolerance(
    timestamp: datetime | None,
    *,
    now: datetime | None = None,
    tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE,
) -> timedelta | None:
    """Return the excessive future offset, or ``None`` when the clock skew is tolerated.

    The two-minute default is deliberately small but non-zero so harmless differences between
    the local clock and an observation source do not invalidate otherwise current evidence.
    Callers use this same helper for trust decisions and user-facing age text.
    """

    if tolerance < timedelta(0):
        raise ValueError("clock-skew tolerance cannot be negative")
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        raise ValueError("observation timestamps must be timezone-aware")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    offset = timestamp - current
    return offset if offset > tolerance else None


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Classify an observation without silently changing its trust status.

    ``AGING`` is informational and remains within the accepted maximum age.
    The default threshold is halfway to ``max_age`` so the policy remains
    deterministic even when the caller only supplies one user-facing limit.
    """

    max_age: timedelta = timedelta(hours=4)
    aging_after: timedelta | None = None
    clock_skew_tolerance: timedelta = DEFAULT_CLOCK_SKEW_TOLERANCE

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if self.aging_after is not None and not timedelta(0) < self.aging_after < self.max_age:
            raise ValueError("aging_after must be positive and less than max_age")
        if self.clock_skew_tolerance < timedelta(0):
            raise ValueError("clock_skew_tolerance cannot be negative")

    def classify(self, timestamp: datetime | None, *, now: datetime | None = None) -> Freshness:
        if timestamp is None:
            return Freshness.UNKNOWN
        if timestamp.tzinfo is None:
            raise ValueError("observation timestamps must be timezone-aware")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if (
            future_offset_beyond_tolerance(
                timestamp,
                now=current,
                tolerance=self.clock_skew_tolerance,
            )
            is not None
        ):
            return Freshness.FUTURE
        age = max(current - timestamp, timedelta(0))
        if age > self.max_age:
            return Freshness.STALE
        threshold = self.aging_after or self.max_age / 2
        if age > threshold:
            return Freshness.AGING
        return Freshness.FRESH
