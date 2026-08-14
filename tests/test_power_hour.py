"""Tests for Power Hour 15m VWAP + confluence desk."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from odte_scanner.signals.power_hour import (
    build_power_hour_board,
    decide_power_hour,
    resolve_power_hour_symbols,
    seed_quotes_from_scan,
    session_phase,
    top_closing_bell_bullish,
)

ET = ZoneInfo("America/New_York")


def test_session_phase_power_hour():
    assert session_phase(datetime(2026, 8, 14, 15, 10, tzinfo=ET)) == "power_hour"
    assert session_phase(datetime(2026, 8, 14, 14, 40, tzinfo=ET)) == "prep"


def test_resolve_includes_specials_priority_and_focus():
    cfg = {"tickers": ["SPY", "TSLA", "NVDA", "AAPL", "NU", "CAPR"], "actions": {"power_hour_symbols": "focus"}}
    syms = resolve_power_hour_symbols("focus", config=cfg)
    for s in ("NU", "NVDA", "CAPR", "ETON", "HTFL", "TSLA", "GOOGL", "NXPI", "SPY", "AAPL", "NBIS", "CRWV", "AVGO", "IWM"):
        assert s in syms


def test_nxpi_requires_qqq():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    blocked = decide_power_hour(
        "NXPI",
        quote={"last": 220.0, "day_high": 222.0, "day_low": 215.0, "mom_15m_pct": 0.25, "vwap": 218.0},
        qqq_quote={"last": 400.0, "day_high": 402.0, "day_low": 398.0},
        qqq_vwap=401.0,
        phase="power_hour",
        now=now,
    )
    assert blocked.action != "LONG"
    ok = decide_power_hour(
        "NXPI",
        quote={"last": 220.0, "day_high": 222.0, "day_low": 215.0, "mom_15m_pct": 0.25, "vwap": 218.0},
        qqq_quote={"last": 480.0, "day_high": 482.0, "day_low": 475.0},
        qqq_vwap=478.0,
        phase="power_hour",
        now=now,
    )
    assert ok.action == "LONG"
    assert ok.special is True


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
    assert sig.action in {"WATCH", "WAIT"}


def test_nu_long_above_vwap():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
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
        qqq_vwap=401.0,
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
    assert board["primary"] is not None
    assert board["primary"]["action"] in {"LONG", "SHORT"}
    assert board["data_quality"]["tape_ok"] is True
    assert any(r["symbol"] == "TSLA" and r["action"] == "SHORT" for r in board["short"])
    assert "leaders" in board


def test_empty_tape_does_not_pin_nu_primary():
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    board = build_power_hour_board(
        quotes={},
        symbols=["NU", "NVDA", "TSLA"],
        fetch_bars=False,
        now=now,
    )
    assert board["primary"] is None
    assert board["data_quality"]["tape_ok"] is False
    assert board["counts"]["with_last"] == 0


def test_seed_quotes_from_scan_scores():
    seeded = seed_quotes_from_scan(
        {},
        scores=[{"symbol": "MU", "last_price": 965.0}, {"symbol": "AAPL", "entry": 230.0}],
        market={"by_score": [{"symbol": "MU", "last": 966.0, "change_pct": 1.5}]},
    )
    assert seeded["MU"]["last"] == 965.0
    assert seeded["AAPL"]["last"] == 230.0
    assert seeded["MU"]["session_change_pct"] == 1.5


def test_closing_bell_bullish_prefers_movers():
    flow = {
        "prints": [
            {
                "symbol": "ZZZ",
                "flow_score": 99,
                "sentiment": "bullish",
                "tier": "golden",
                "right": "C",
                "strike": 10,
                "expiry": "2026-08-17",
                "premium_notional": 1e6,
            },
            {
                "symbol": "MU",
                "flow_score": 88,
                "sentiment": "bullish",
                "tier": "golden",
                "right": "C",
                "strike": 965,
                "expiry": "2026-08-17",
                "premium_notional": 6e6,
            },
            {
                "symbol": "AAPL",
                "flow_score": 82,
                "sentiment": "bullish",
                "tier": "golden",
                "right": "C",
                "strike": 305,
                "expiry": "2026-08-17",
                "premium_notional": 1.5e6,
            },
        ]
    }
    market = {
        "by_score": [
            {"symbol": "MU", "change_pct": 1.6},
            {"symbol": "AAPL", "change_pct": 0.4},
            {"symbol": "XLE", "change_pct": 1.2},
        ]
    }
    top = top_closing_bell_bullish(option_flow=flow, market=market, n=2)
    assert [r["symbol"] for r in top["rows"]] == ["MU", "AAPL"]
    assert top["rows"][0]["in_movers"] is True
    assert "ZZZ" not in {r["symbol"] for r in top["rows"]}


def test_confluence_promotes_nbis_leader():
    """NBIS-style mover + bullish flow + score should surface as LONG / leader without 15m bars."""
    now = datetime(2026, 8, 14, 15, 20, tzinfo=ET)
    board = build_power_hour_board(
        quotes={
            "NBIS": {"last": 273.0, "day_high": 275.0, "day_low": 250.0, "session_change_pct": 7.0, "vwap": 260.0},
            "IWM": {"last": 305.0, "day_high": 306.0, "day_low": 302.0, "session_change_pct": 0.5, "vwap": 303.5},
            "CRWV": {"last": 106.0, "day_high": 108.0, "day_low": 104.0, "session_change_pct": -0.4, "vwap": 106.5},
            "AVGO": {"last": 390.0, "day_high": 410.0, "day_low": 388.0, "session_change_pct": -5.5, "vwap": 400.0},
            "GOOGL": {"last": 345.0, "day_high": 348.0, "day_low": 343.0, "session_change_pct": -0.3, "vwap": 346.0},
            "QQQ": {"last": 480.0, "day_high": 482.0, "day_low": 475.0, "vwap": 478.0},
        },
        symbols=["NBIS", "IWM", "CRWV", "AVGO", "GOOGL", "NU"],
        fetch_bars=False,
        now=now,
        scores=[
            {"symbol": "NBIS", "ensemble_score": 69, "bullish": True, "last_price": 273.0},
            {"symbol": "IWM", "ensemble_score": 68, "bullish": True, "last_price": 305.0},
            {"symbol": "CRWV", "ensemble_score": 61, "bullish": True, "last_price": 106.0},
            {"symbol": "AVGO", "ensemble_score": 48, "bullish": False, "last_price": 390.0},
            {"symbol": "GOOGL", "ensemble_score": 46, "bullish": False, "last_price": 345.0},
        ],
        market={
            "by_score": [
                {"symbol": "NBIS", "change_pct": 7.0, "last": 273.0},
                {"symbol": "IWM", "change_pct": 0.5, "last": 305.0},
                {"symbol": "CRWV", "change_pct": -0.4, "last": 106.0},
                {"symbol": "AVGO", "change_pct": -5.5, "last": 390.0},
                {"symbol": "GOOGL", "change_pct": -0.3, "last": 345.0},
            ]
        },
        option_flow={
            "prints": [
                {"symbol": "NBIS", "flow_score": 76, "sentiment": "bullish", "tier": "golden", "right": "C", "strike": 272.5, "expiry": "2026-08-21", "premium_notional": 1.3e6},
                {"symbol": "AVGO", "flow_score": 83, "sentiment": "bullish", "tier": "golden", "right": "C", "strike": 395, "expiry": "2026-08-17", "premium_notional": 2e6},
            ]
        },
        dealer_edge={
            "profiles": [
                {"symbol": "NBIS", "regime": "negative_gamma", "net_gex": -1e6, "flip": 260, "put_wall": 250, "call_wall": 280, "spot": 273},
                {"symbol": "AVGO", "regime": "positive_gamma", "net_gex": 2e6, "flip": 400, "put_wall": 390, "call_wall": 410, "spot": 390},
            ]
        },
    )
    by = {r["symbol"]: r for r in board["all"]}
    assert by["NBIS"]["action"] == "LONG"
    assert by["NBIS"]["playbook"] == "confluence"
    assert by["NBIS"]["gex_bias"] == "trend"
    leader_syms = {r["symbol"] for r in board["leaders"]}
    for s in ("NBIS", "IWM", "CRWV", "AVGO", "GOOGL"):
        assert s in leader_syms
    # AVGO dumped hard — should not be a blind LONG even with bullish flow
    assert by["AVGO"]["action"] != "LONG"
