"""Tests for Power Hour 15m VWAP desk."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from odte_scanner.signals.power_hour import (
    build_power_hour_board,
    decide_power_hour,
    resolve_power_hour_symbols,
    session_phase,
)

ET = ZoneInfo("America/New_York")


def test_session_phase_power_hour():
    assert session_phase(datetime(2026, 8, 14, 15, 10, tzinfo=ET)) == "power_hour"
    assert session_phase(datetime(2026, 8, 14, 14, 40, tzinfo=ET)) == "prep"


def test_resolve_includes_specials_and_focus():
    cfg = {"tickers": ["SPY", "TSLA", "NVDA", "AAPL", "NU", "CAPR"], "actions": {"power_hour_symbols": "focus"}}
    syms = resolve_power_hour_symbols("focus", config=cfg)
    for s in ("NU", "NVDA", "CAPR", "ETON", "HTFL", "TSLA", "GOOGL", "SPY", "AAPL"):
        assert s in syms


def test_no_new_entries_after_1545():
    now = datetime(2026, 8, 14, 15, 50, tzinfo=ET)
    sig = decide_power_hour(
        "NU",
        quote={"last": 12.5, "day_high": 12.6, "day_low": 11.8, "mom_15m_pct": 0.3, "vwap": 12.0},
        phase="power_hour",
        now=now,
    )
    assert sig.action == "WAIT"
    assert any("15:45" in r or "flatten" in r.lower() for r in sig.reasons)


def test_googl_requires_qqq():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    sig = decide_power_hour(
        "GOOGL",
        quote={"last": 180.0, "day_high": 181.0, "day_low": 176.0, "mom_15m_pct": 0.2, "vwap": 178.0},
        qqq_quote={"last": 400.0, "day_high": 402.0, "day_low": 398.0},
        qqq_vwap=401.0,
        phase="power_hour",
        now=now,
    )
    assert sig.action != "LONG"


def test_vwap_chop_dead_zone():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    sig = decide_power_hour(
        "AAPL",
        quote={"last": 100.05, "day_high": 101.0, "day_low": 99.0, "mom_15m_pct": 0.2, "vwap": 100.0},
        qqq_quote={"last": 480.0, "vwap": 478.0},
        qqq_vwap=478.0,
        phase="power_hour",
        now=now,
    )
    # |vs VWAP| ~0.05% → chop
    assert sig.action in {"WATCH", "WAIT"}


def test_nu_long_above_vwap():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    # Synthetic 1m bars above VWAP with rising close
    idx = pd.date_range("2026-08-14 14:00", periods=60, freq="1min", tz=ET)
    closes = [10 + i * 0.01 for i in range(60)]
    df = pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 0.05 for c in closes],
            "Low": [c - 0.02 for c in closes],
            "Close": closes,
            "Volume": [1000] * 60,
        },
        index=idx,
    )
    sig = decide_power_hour(
        "NU",
        quote={"last": closes[-1], "day_high": closes[-1] + 0.1, "mom_15m_pct": 0.3},
        bars_1m=df,
        phase="power_hour",
        now=now,
    )
    assert sig.action == "LONG"
    assert "VWAP" in sig.risk_line or "vwap" in sig.risk_line.lower() or "VWAP" in sig.trigger


def test_nvda_requires_qqq_above_vwap():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    sig = decide_power_hour(
        "NVDA",
        quote={"last": 120.0, "day_high": 121.0, "day_low": 118.0, "mom_15m_pct": 0.2, "vwap": 119.0},
        qqq_quote={"last": 400.0, "day_high": 402.0, "day_low": 398.0},
        qqq_vwap=401.0,  # QQQ below VWAP
        phase="power_hour",
        now=now,
    )
    assert sig.action != "LONG"


def test_generic_short_below_vwap():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    sig = decide_power_hour(
        "TSLA",
        quote={"last": 240.0, "day_high": 250.0, "day_low": 238.0, "mom_15m_pct": -0.4, "vwap": 245.0},
        phase="power_hour",
        now=now,
    )
    assert sig.action == "SHORT"
    assert sig.special is True


def test_board_long_short_buckets():
    now = datetime(2026, 8, 14, 15, 15, tzinfo=ET)
    board = build_power_hour_board(
        quotes={
            "NU": {"last": 12.5, "day_high": 12.6, "day_low": 11.8, "mom_15m_pct": 0.3, "vwap": 12.0},
            "TSLA": {"last": 240.0, "day_high": 250.0, "day_low": 238.0, "mom_15m_pct": -0.5, "vwap": 245.0},
            "QQQ": {"last": 480.0, "day_high": 482.0, "day_low": 475.0, "vwap": 478.0},
            "NVDA": {"last": 120.0, "day_high": 121.0, "day_low": 118.0, "mom_15m_pct": 0.25, "vwap": 119.0},
        },
        symbols=["NU", "NVDA", "TSLA", "CAPR", "ETON", "HTFL", "AAPL"],
        fetch_bars=False,
        now=now,
    )
    assert board["counts"]["names"] >= 6
    assert board["session_phase"] == "power_hour"
    actions = {r["symbol"]: r["action"] for r in board["all"]}
    assert actions.get("TSLA") == "SHORT"
    assert "long" in board and "short" in board
    assert board["special_rules"]["CAPR"]["risk"]
