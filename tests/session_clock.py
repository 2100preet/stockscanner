"""Weekday session clocks so option fixtures stay valid on any calendar day."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def session_et(hour: int = 11, minute: int = 0, *, weekday_offset: int = 0) -> datetime:
    """A weekday US/Eastern session.

    Today when today is a weekday, otherwise the upcoming Monday, at
    ``hour:minute``. ``weekday_offset`` walks that many additional weekdays
    forward (a weekly expiry sitting after the 0DTE session).
    """
    day = datetime.now(ET).date()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    stepped = 0
    while stepped < weekday_offset:
        day += timedelta(days=1)
        if day.weekday() < 5:
            stepped += 1
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def iso_day(when: datetime) -> str:
    return when.date().isoformat()
