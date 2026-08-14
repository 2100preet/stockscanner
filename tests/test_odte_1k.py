"""Tests for 0DTE $1K Challenge + ORB15."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from odte_scanner.challenge.odte_1k import (
    build_odte_1k_board,
    decide_odte_1k_entry,
    resolve_odte_1k_symbols,
)
from odte_scanner.challenge.odte_1k_tracker import Odte1kTracker
from odte_scanner.challenge.orb15 import Orb15Levels, classify_vs_orb, compute_orb15

ET = ZoneInfo("America/New_York")


def _orb_bars(day: str = "2026-08-14") -> pd.DataFrame:
    """Synthetic 1m bars: ORB high 778.2 / low 777.66."""
    start = pd.Timestamp(f"{day} 09:30:00", tz=ET)
    rows = []
    idx = []
    for i in range(15):
        ts = start + pd.Timedelta(minutes=i)
        idx.append(ts)
        low = 777.66 if i == 7 else 777.80
        high = 778.20 if i == 3 else 778.00
        rows.append({"Open": 777.90, "High": high, "Low": low, "Close": 777.85, "Volume": 1000})
    for i in range(15, 40):
        ts = start + pd.Timedelta(minutes=i)
        idx.append(ts)
        rows.append({"Open": 777.50, "High": 777.60, "Low": 776.40, "Close": 776.50, "Volume": 800})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def test_compute_orb15_ready():
    day = "2026-08-14"
    bars = _orb_bars(day)
    now = datetime(2026, 8, 14, 10, 5, tzinfo=ET)
    orb = compute_orb15(bars, symbol="SPY", session_day=datetime(2026, 8, 14).date(), now=now)
    assert orb.status == "ready"
    assert orb.low == 777.66
    assert orb.high == 778.2


def test_classify_break_hold_and_retest():
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=778.2,
        low=777.66,
        status="ready",
    )
    vs = classify_vs_orb(776.50, orb)
    assert vs["broke_orb_low"] is True
    assert vs["holds_below_low"] is True
    vs2 = classify_vs_orb(777.50, orb, retest_band_usd=0.40)
    assert vs2["retest_orb_low"] is True


def test_put_now_on_break_hold_green_friday():
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=778.2,
        low=777.66,
        status="ready",
        bars=15,
    )
    now = datetime(2026, 8, 14, 10, 20, tzinfo=ET)
    sig = decide_odte_1k_entry(
        orb=orb,
        quote={"last": 776.50, "session_change_pct": 0.4, "mom_5m_pct": -0.12},
        symbol="SPY",
        red_flag={"state": "SUPPORTIVE", "block_0dte_long_calls": False},
        actions={"buy_now": [{"symbol": "SPY", "right": "C", "action": "BUY_NOW"}]},
        fetch_contract=False,
        now=now,
    )
    assert sig.action == "PUT_NOW"
    assert sig.green_friday is True
    assert sig.broke_orb_low is True
    assert sig.call_safe_zone_conflict is True
    assert sig.signaled_at_cst
    assert "Green Friday" in sig.detail or any("Green Friday" in r for r in sig.reasons)


def test_watch_when_inside_range():
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=778.2,
        low=777.66,
        status="ready",
        bars=15,
    )
    now = datetime(2026, 8, 14, 10, 20, tzinfo=ET)
    sig = decide_odte_1k_entry(
        orb=orb,
        quote={"last": 777.90, "session_change_pct": 0.3, "mom_5m_pct": 0.02},
        fetch_contract=False,
        now=now,
    )
    assert sig.action == "WATCH"


def test_resolve_includes_user_names_and_now():
    cfg = {
        "tickers": ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "NBIS", "AAPL", "SLV", "SPCX", "NOW", "PLTR"],
        "actions": {"odte_1k_symbols": "focus"},
    }
    syms = resolve_odte_1k_symbols("focus", config=cfg)
    for need in ("SPY", "IWM", "TSLA", "NVDA", "NBIS", "AAPL", "SLV", "SPCX", "NOW"):
        assert need in syms
    assert syms.index("SPY") < syms.index("PLTR")


def test_put_now_action_is_emitted():
    """PUT NOW is the actionable entry (user asked if NOW is included — ticker + action)."""
    orb = Orb15Levels(
        symbol="NOW",
        session_date="2026-08-14",
        high=900.0,
        low=890.0,
        status="ready",
        bars=15,
    )
    now = datetime(2026, 8, 14, 10, 20, tzinfo=ET)
    sig = decide_odte_1k_entry(
        orb=orb,
        quote={"last": 888.0, "session_change_pct": 0.5, "mom_5m_pct": -0.2},
        symbol="NOW",
        fetch_contract=False,
        now=now,
    )
    assert sig.action == "PUT_NOW"
    assert sig.symbol == "NOW"


def test_board_builds_with_injected_orb():
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=778.2,
        low=777.66,
        status="ready",
        bars=15,
    )
    now = datetime(2026, 8, 14, 10, 20, tzinfo=ET)
    board = build_odte_1k_board(
        quotes={"SPY": {"last": 776.5, "session_change_pct": 0.5, "mom_5m_pct": -0.2}},
        symbols=["SPY"],
        orb_map={"SPY": orb},
        fetch_bars=False,
        fetch_contracts=False,
        now=now,
    )
    assert board["counts"]["put_now"] >= 1
    assert board["orb"]["SPY"]["low"] == 777.66
    assert board["green_friday"] is True


def test_tracker_two_trade_day_cap(tmp_path):
    path = tmp_path / "odte_1k.json"
    tr = Odte1kTracker(path, starting_cash=1000, max_trades_per_day=2, default_size_usd=850)
    sig = {
        "symbol": "SPY",
        "right": "P",
        "ask": 2.0,
        "strike": 776,
        "expiry": "2026-08-14",
        "contract": "SPY260814P00776000",
        "spot": 776.5,
        "detail": "PUT NOW test",
        "position_size_usd": 850,
        "orb_low": 777.66,
        "orb_high": 778.2,
        "green_friday": True,
    }
    t1 = tr.enter(sig)
    assert t1 is not None
    assert t1.cost == 800.0  # 4 contracts * 2 * 100 = 800 under $850 budget
    t2 = tr.enter({**sig, "ask": 1.5})
    assert t2 is not None
    t3 = tr.enter({**sig, "ask": 1.0})
    assert t3 is None  # day cap
    out = tr.exit_trade(t1.id, exit_bid=3.0, reason="target")
    assert out is not None
    assert out.pnl_usd == 400.0
    book = tr.book.to_dict()
    assert book["wins"] == 1
    assert book["equity"] > 1000
