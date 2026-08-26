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
    assert sig.side == "IN"
    assert sig.desk_action == "BUY_PUT"
    assert sig.alert_action == "BUY_NOW"
    assert sig.ask is not None and sig.ask > 0
    assert sig.green_friday is True
    assert sig.broke_orb_low is True
    assert sig.call_safe_zone_conflict is True
    assert sig.signaled_at_cst
    assert "Green Friday" in sig.detail or any("Green Friday" in r for r in sig.reasons)
    d = sig.to_dict()
    assert d["side"] == "IN"
    assert d["desk_action"] == "BUY_PUT"


def test_proxy_orb_never_fires_in():
    """Day H/L proxy must not arm IN — was the main reason desk stayed empty."""
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=780.0,
        low=770.0,
        status="proxy",
        note="Day H/L proxy",
    )
    now = datetime(2026, 8, 14, 10, 20, tzinfo=ET)
    sig = decide_odte_1k_entry(
        orb=orb,
        quote={"last": 769.0, "session_change_pct": 0.4, "mom_5m_pct": -0.2},
        fetch_contract=False,
        now=now,
    )
    assert sig.action == "WATCH"
    assert sig.side == "WATCH"


def test_exit_is_out_sell_put():
    orb = Orb15Levels(
        symbol="SPY",
        session_date="2026-08-14",
        high=778.2,
        low=777.66,
        status="ready",
        bars=15,
    )
    now = datetime(2026, 8, 14, 15, 50, tzinfo=ET)
    sig = decide_odte_1k_entry(
        orb=orb,
        quote={"last": 776.0, "session_change_pct": 0.2, "mom_5m_pct": -0.1},
        open_trade={
            "status": "open",
            "entry_ask": 1.5,
            "mark": 1.6,
            "strike": 776,
            "contracts": 2,
            "cost": 300,
        },
        now=now,
    )
    assert sig.action == "EXIT"
    assert sig.side == "OUT"
    assert sig.desk_action == "SELL_PUT"
    assert sig.alert_action == "SELL_NOW"


def test_board_emits_in_out_aliases():
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
    assert board["counts"]["in"] >= 1
    assert board["entry"]
    assert board["in"][0]["side"] == "IN"
    assert board["in"][0]["desk_action"] == "BUY_PUT"
    assert board["in"][0]["ask"] is not None


def test_backtest_orb15_puts_fires_and_exits():
    from odte_scanner.challenge.odte_1k_backtest import backtest_orb15_puts

    day = "2026-08-14"
    bars = _orb_bars(day)
    # Extend with reclaim then flatten path
    start = pd.Timestamp(f"{day} 10:10:00", tz=ET)
    extra = []
    idx = []
    for i in range(40):
        ts = start + pd.Timedelta(minutes=i)
        idx.append(ts)
        # stay below ORB then reclaim late
        close = 776.2 if i < 20 else 778.5
        extra.append({"Open": close, "High": close + 0.1, "Low": close - 0.1, "Close": close, "Volume": 500})
    more = pd.DataFrame(extra, index=pd.DatetimeIndex(idx))
    bars = pd.concat([bars, more])
    # Force green ORB open < ORB close for green filter
    bars.loc[bars.index[0], "Open"] = 777.50
    result = backtest_orb15_puts(bars, symbol="SPY", hold_bars=2, require_green=True)
    assert result.sessions >= 1
    assert result.trades >= 1
    assert result.trade_rows[0].exit_reason in {"reclaim_orb_low", "target", "stop", "flatten", "session_end"}
    assert result.win_pct >= 0


def test_rec_log_sync_odte_1k_in_out(tmp_path):
    from odte_scanner.trading.rec_log import RecommendationLog

    log = RecommendationLog(tmp_path / "rec.json")
    n = log.sync_odte_1k(
        {
            "put_now": [
                {
                    "symbol": "SPY",
                    "right": "P",
                    "action": "PUT_NOW",
                    "ask": 1.4,
                    "strike": 776,
                    "expiry": "2026-08-14",
                    "spot": 776.5,
                    "headline": "IN · BUY PUT SPY",
                    "detail": "break+hold",
                }
            ],
            "exit_now": [],
            "hold": [],
            "watch": [],
        }
    )
    assert n >= 1
    board = log.board(section="odte_1k")
    assert board["open"] >= 1
    assert board["open_recs"][0]["symbol"] == "SPY"
    # OUT closes with P&L
    log.sync_odte_1k(
        {
            "put_now": [],
            "exit_now": [
                {
                    "symbol": "SPY",
                    "right": "P",
                    "action": "EXIT",
                    "bid": 2.2,
                    "spot": 774.0,
                    "detail": "OUT · bank",
                }
            ],
            "hold": [],
            "watch": [],
        }
    )
    board2 = log.board(section="odte_1k")
    assert board2["closed"] >= 1
    closed = board2["closed_recs"][0]
    assert closed["profit_pct"] is not None
    assert closed["profit_pct"] > 0



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
    assert sig.side == "IN"
    assert "BUY PUT" in sig.headline


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
    assert board["counts"]["in"] >= 1
    assert board["orb"]["SPY"]["low"] == 777.66
    assert board["green_friday"] is True
    assert board["put_now"][0]["ask"] is not None


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
