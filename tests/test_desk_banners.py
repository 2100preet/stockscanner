"""Diamond and Sniper stay on the desk when nothing has triggered yet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import odte_scanner.edge.diamond_study  # noqa: F401
from odte_scanner.signals.diamond import Bar
from odte_scanner.signals.diamond_board import build_diamond_board
from odte_scanner.signals.sniper_board import build_sniper_board
from odte_scanner.ui import PAGE

CT = ZoneInfo("America/Chicago")


def _bar(day: str, hh: int, mm: int, high: float, low: float) -> Bar:
    ts = datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=CT)
    mid = (high + low) / 2
    return Bar(ts=ts, open=mid, high=high, low=low, close=mid, volume=1000)


def _range_day(day: str = "2026-10-28") -> list[Bar]:
    bars = [
        _bar(day, 8, 30, 100.40, 99.80),
        _bar(day, 8, 35, 100.20, 99.90),
        _bar(day, 8, 40, 100.10, 99.70),
        _bar(day, 8, 45, 100.00, 99.85),
        _bar(day, 8, 50, 100.05, 99.88),
        _bar(day, 8, 55, 100.15, 99.92),
    ]
    cursor = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=CT)
    end = datetime.fromisoformat(f"{day}T10:30:00").replace(tzinfo=CT)
    while cursor <= end:
        bars.append(_bar(day, cursor.hour, cursor.minute, 100.20, 100.00))
        cursor += timedelta(minutes=5)
    return bars


def test_banners_stay_visible_with_a_waiting_line():
    assert "function deskStatusLine" in PAGE
    assert "Waiting — window opens 9:00 CT" in PAGE
    assert "Waiting — window opens 8:45 CT" in PAGE
    assert "Scanning SPY QQQ … — no trigger yet" in PAGE
    assert "No Diamond today" in PAGE
    assert "No Sniper today" in PAGE
    assert "pulse-banner.diamond" in PAGE
    assert "pulse-banner.sniper" in PAGE
    diamond = PAGE[PAGE.index("function renderDiamondBanner") : PAGE.index("function renderSniperBanner")]
    sniper = PAGE[PAGE.index("function renderSniperBanner") : PAGE.index("function paint")]
    assert 'el.classList.remove("active")' not in diamond
    assert 'el.classList.remove("active")' not in sniper
    assert 'el.classList.add("active", "diamond")' in diamond
    assert 'el.classList.add("active", "sniper")' in sniper
    assert "pulse-card diamond" in diamond
    assert "pulse-card sniper" in sniper


def _until(bars: list[Bar], hh: int, mm: int) -> list[Bar]:
    return [bar for bar in bars if (bar.ct().hour, bar.ct().minute) <= (hh, mm)]


def test_opening_range_is_published_once_the_last_bar_prints(tmp_path):
    day = "2026-10-28"
    full = _range_day(day)
    early = {"SPY": _until(full, 8, 35), "QQQ": _until(full, 8, 35)}
    before = datetime(2026, 10, 28, 13, 40, tzinfo=timezone.utc)  # 8:40 CT
    sniper_early = build_sniper_board(early, now=before, store_path=tmp_path / "s1.json", chains={})
    assert sniper_early["window_open"] is False
    assert sniper_early["opening_range"] == {}
    at_open = datetime(2026, 10, 28, 13, 50, tzinfo=timezone.utc)  # 8:50 CT
    mid = {"SPY": _until(full, 8, 50), "QQQ": _until(full, 8, 50)}
    sniper = build_sniper_board(mid, now=at_open, store_path=tmp_path / "s2.json", chains={})
    assert sniper["opening_range"]["SPY"] == {"high": 100.40, "low": 99.70}
    assert sniper["opening_range"]["QQQ"]["high"] == 100.40
    diamond_waiting = build_diamond_board(mid, now=at_open, store_path=tmp_path / "d1.json", chains={})
    assert diamond_waiting["window_open"] is False
    assert diamond_waiting["opening_range"] == {}
    after_nine = datetime(2026, 10, 28, 14, 5, tzinfo=timezone.utc)  # 9:05 CT
    diamond = build_diamond_board(
        {"SPY": full, "QQQ": full}, now=after_nine, store_path=tmp_path / "d2.json", chains={}
    )
    assert diamond["window_open"] is True
    assert diamond["opening_range"]["SPY"] == {"high": 100.40, "low": 99.70}
