"""A clock you can freeze.

Reproducibility is a stated requirement of the eval harness: the same seed
must produce the same incidents. Anything reading `datetime.now()` directly
breaks that, so services take a `Clock` and tests pass a `FrozenClock`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    """Wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock(Clock):
    """Manually advanced clock for simulations and tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=UTC)


SYSTEM_CLOCK = Clock()


def utc(value: str | datetime) -> datetime:
    """Coerce anything we read back out of SQLite into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
