"""Exit-study replay: closes, not highs, decide the fill."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.backtest.exit_replay import replay_exit

ET = ZoneInfo("America/New_York")


def _bar(hour: int, minute: int, close: float, *, high: float | None = None, low: float | None = None) -> dict:
    ts = int(datetime(2026, 10, 14, hour, minute, tzinfo=ET).timestamp())
    return {"t": ts, "h": high if high is not None else close, "l": low if low is not None else close, "c": close}


def test_tp30_sells_before_the_later_spike():
    bars = [
        _bar(10, 0, 1.00),
        _bar(10, 5, 1.35, high=1.40),
        _bar(10, 10, 2.50),
    ]
    out = replay_exit(1.0, bars, "tp30", entry_ts=datetime(2026, 10, 14, 10, 0, tzinfo=ET))
    assert out is not None
    assert out["why"] == "tp"
    assert out["exit"] == 1.35
    assert out["pnl"] == pytest.approx(35)


def test_current_intraday_stops_and_eod_holds_the_recovery():
    bars = [
        _bar(10, 0, 1.00),
        _bar(11, 0, 0.40, low=0.40),
        _bar(15, 50, 1.80),
    ]
    entry = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
    stopped = replay_exit(1.0, bars, "current", entry_ts=entry, intraday=True)
    held = replay_exit(1.0, bars, "eod", entry_ts=entry, intraday=True)
    assert stopped is not None and stopped["why"] == "sl"
    assert stopped["pnl"] == pytest.approx(-60)
    assert held is not None and held["why"] == "eod"
    assert held["exit"] == 1.80


def test_trail_gives_back_from_the_high():
    bars = [
        _bar(10, 0, 1.00),
        _bar(10, 20, 1.30, high=1.40),
        _bar(10, 30, 1.05),
    ]
    out = replay_exit(1.0, bars, "trail", entry_ts=datetime(2026, 10, 14, 10, 0, tzinfo=ET))
    assert out is not None
    assert out["why"] == "trail"
    assert out["exit"] == 1.05
