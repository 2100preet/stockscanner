"""Signals use closed 5-minute bars only. The feed includes the forming bar.

Nov 10: Diamond bought GOOGL at 9:00:03 and MU at 9:00:23 on a 9:00 bar a few
seconds old, and Sniper alerted META at 9:19:23 on the 9:15 bar. None of them
qualified once the bar closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import odte_scanner.edge  # noqa: F401  load first: signals.diamond <-> edge import cycle
from odte_scanner.fast_desk import BAR_CLOSE_OFFSET_SEC, next_cycle_sleep
from odte_scanner.signals.diamond import Bar
from odte_scanner.signals.diamond_board import build_diamond_board, closed_bars
from odte_scanner.signals.sniper_board import build_sniper_board

from tests.test_diamond import _bar as _dbar, _trend_day
from tests.test_sniper import _bar as _sbar, _call_day

CT = ZoneInfo("America/Chicago")
DAY = "2026-10-27"


def _ct(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 27, hh, mm, ss, tzinfo=CT)


def _upto(bars: list[Bar], last: datetime) -> list[Bar]:
    """What the feed returns at ``last``: every bar that has started, forming one included."""
    return [bar for bar in bars if bar.ct() <= last]


def _chain(symbols, right="C"):
    return {s: [{"right": right, "strike": 100, "expiry": "2026-10-29", "ask": 1.4, "bid": 1.3}] for s in symbols}


def test_closed_bars_drops_the_forming_bar_and_keeps_one_exactly_closed():
    bars = {"SPY": [_dbar(DAY, 9, 0, 100.0), _dbar(DAY, 9, 5, 100.1)]}
    at = closed_bars(bars, _ct(9, 5, 3))
    assert [b.ct().strftime("%H:%M") for b in at["SPY"]] == ["09:00"]
    # Exactly at the boundary the 9:00 bar is complete; 9:05 is zero seconds old.
    assert [b.ct().strftime("%H:%M") for b in closed_bars(bars, _ct(9, 5))["SPY"]] == ["09:00"]
    assert closed_bars(bars, _ct(9, 4, 59))["SPY"] == []
    # Naive now is CT, same as the boards; UTC-aware now compares by instant.
    assert len(closed_bars(bars, datetime(2026, 10, 27, 9, 10, 0))["SPY"]) == 2
    assert len(closed_bars(bars, datetime(2026, 10, 27, 14, 10, tzinfo=timezone.utc))["SPY"]) == 2
    assert closed_bars({}, _ct(9, 0)) == {}
    assert closed_bars({"SPY": []}, _ct(9, 0)) == {"SPY": []}


def test_diamond_waits_for_the_9_00_bar_to_close(tmp_path):
    day = _trend_day()
    store = tmp_path / "diamond_day.json"
    early = build_diamond_board({"SPY": _upto(day, _ct(9, 0, 3))}, now=_ct(9, 0, 3), store_path=store, chains=_chain(["SPY"]))
    assert early["signals"] == []
    closed = build_diamond_board({"SPY": _upto(day, _ct(9, 5, 3))}, now=_ct(9, 5, 3), store_path=store, chains=_chain(["SPY"]))
    assert [row["symbol"] for row in closed["signals"]] == ["SPY"]
    assert closed["signals"][0]["detected_at_ct"] == "9:05:03"
    assert closed["signals"][0]["send"] is True


def test_diamond_forming_breakout_that_fails_on_close_never_fires(tmp_path):
    day = _trend_day()
    store = tmp_path / "diamond_day.json"
    # 3 seconds in, the 9:00 bar is through the range ...
    forming = _upto(day, _ct(9, 0))
    first = build_diamond_board({"SPY": forming}, now=_ct(9, 0, 3), store_path=store, chains=_chain(["SPY"]))
    assert first["signals"] == []
    # ... then closes back inside it, and 9:05 drifts under the range high.
    faded = day[:6] + [_dbar(DAY, 9, 0, 99.8, high=100.25, low=99.7), _dbar(DAY, 9, 5, 99.75, high=99.85, low=99.6)]
    after = build_diamond_board({"SPY": faded}, now=_ct(9, 5, 3), store_path=store, chains=_chain(["SPY"]))
    assert after["signals"] == []


def test_sniper_waits_for_the_retest_bar_to_close(tmp_path):
    day = _call_day()
    store = tmp_path / "sniper_day.json"
    # Window opens 8:45. The retest bar is 8:50; at 8:50:03 it is forming.
    early = build_sniper_board({"SPY": _upto(day, _ct(8, 50, 3))}, now=_ct(8, 50, 3), store_path=store, chains=_chain(["SPY"]))
    assert early["signals"] == []
    closed = build_sniper_board({"SPY": _upto(day, _ct(8, 55, 3))}, now=_ct(8, 55, 3), store_path=store, chains=_chain(["SPY"]))
    assert [row["symbol"] for row in closed["signals"]] == ["SPY"]
    assert closed["signals"][0]["at_ct"].startswith("8:50") or closed["signals"][0]["at_ct"].startswith("08:50")
    assert closed["signals"][0]["detected_at_ct"] == "8:55:03"


def test_sniper_forming_retest_that_closes_as_a_chase_never_fires(tmp_path):
    day = _call_day()
    store = tmp_path / "sniper_day.json"
    forming = _upto(day, _ct(8, 50))
    assert build_sniper_board({"SPY": forming}, now=_ct(8, 50, 3), store_path=store, chains=_chain(["SPY"]))["signals"] == []
    # The 8:50 bar closes far above the level: a chase, not a retest.
    chased = day[:4] + [_sbar(DAY, 8, 50, 101.20, high=101.30, low=100.35)]
    assert build_sniper_board({"SPY": chased}, now=_ct(8, 55, 3), store_path=store, chains=_chain(["SPY"]))["signals"] == []


def test_fast_wake_still_lands_three_seconds_after_the_close():
    assert BAR_CLOSE_OFFSET_SEC == 3.0
    # 9:04:50 with a 20s cycle: wake at 9:05:03, the first moment the 9:00 bar is closed.
    sleep = next_cycle_sleep(_ct(9, 4, 50), 0.0, 20.0)
    assert abs(sleep - 13.0) < 1e-6
    woke = _ct(9, 4, 50) + timedelta(seconds=sleep)
    bars = {"SPY": [_dbar(DAY, 9, 0, 100.0), _dbar(DAY, 9, 5, 100.1)]}
    assert [b.ct().strftime("%H:%M") for b in closed_bars(bars, woke)["SPY"]] == ["09:00"]


def test_a_locked_name_keeps_its_slot_after_it_stops_qualifying():
    from odte_scanner.signals import diamond_board, sniper_board

    day = "2026-11-10"
    ranked = [{"symbol": s} for s in ("SPY", "QQQ", "SPX")]
    for module in (diamond_board, sniper_board):
        lock = module._lock_symbols
        assert lock(ranked, {"day": day, "symbols": ["GOOGL", "MU", "META"]}, day) == ["GOOGL", "MU", "META"]
        assert lock(ranked, {"day": day, "symbols": ["GOOGL"]}, day) == ["GOOGL", "SPY", "QQQ"]
        assert lock(ranked, {"day": day, "symbols": ["spy", "SPY"]}, day) == ["SPY", "QQQ", "SPX"]
        # Yesterday's lock does not carry over.
        assert lock(ranked, {"day": "2026-11-09", "symbols": ["GOOGL"]}, day) == ["SPY", "QQQ", "SPX"]


def test_board_does_not_hand_a_fourth_name_to_the_trader(tmp_path):
    import json

    store = tmp_path / "diamond_day.json"
    store.write_text(json.dumps({"day": DAY, "symbols": ["GOOGL", "MU", "META"], "detected": {}}))
    day = _trend_day()
    board = build_diamond_board({"SPY": day}, now=_ct(9, 10, 3), store_path=store, chains=_chain(["SPY"]))
    assert [row for row in board["signals"] if row.get("send")] == []
    assert json.loads(store.read_text())["symbols"] == ["GOOGL", "MU", "META"]
