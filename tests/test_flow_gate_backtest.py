"""Backtest-style tests for Tier-1 ZeroLoss Flow gate (leaders, deltas, journal)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from odte_scanner.echo.flow import build_flow_leaders, build_option_flow
from odte_scanner.echo.flow_deltas import attach_deltas_to_ladder, diff_ladders
from odte_scanner.signals.actions import ActionSignal, build_action_board
from odte_scanner.signals.flow_gate import apply_flow_gate
from odte_scanner.trading.journal import SignalJournal


def _bullish_ladder(symbol: str = "NVDA", *, spot: float = 120.0) -> dict:
    return {
        "symbol": symbol,
        "spot": spot,
        "expiry": "2026-09-05",
        "dte": 3,
        "calls": [
            {
                "right": "C",
                "strike": 120,
                "volume": 22000,
                "open_interest": 5000,
                "mid": 2.0,
                "delta_volume": 8000,
                "delta_oi": 500,
                "premium_delta": 1600000.0,
            }
        ],
        "puts": [
            {
                "right": "P",
                "strike": 115,
                "volume": 800,
                "open_interest": 2000,
                "mid": 1.0,
                "delta_volume": 100,
                "delta_oi": 50,
                "premium_delta": 10000.0,
            }
        ],
    }


def _bearish_ladder(symbol: str = "AMZN", *, spot: float = 180.0) -> dict:
    return {
        "symbol": symbol,
        "spot": spot,
        "expiry": "2026-09-05",
        "dte": 3,
        "calls": [
            {
                "right": "C",
                "strike": 180,
                "volume": 900,
                "open_interest": 3000,
                "mid": 1.5,
                "delta_volume": 200,
                "delta_oi": 100,
                "premium_delta": 30000.0,
            }
        ],
        "puts": [
            {
                "right": "P",
                "strike": 175,
                "volume": 18000,
                "open_interest": 4000,
                "mid": 2.5,
                "delta_volume": 6000,
                "delta_oi": 800,
                "premium_delta": 1500000.0,
            }
        ],
    }


def _buy_now_put(symbol: str = "AMZN") -> ActionSignal:
    return ActionSignal(
        action="BUY_NOW",
        symbol=symbol,
        strength=78.0,
        headline=f"BUY NOW · {symbol} put",
        detail=f"0DTE put sleeve on {symbol}",
        strike=175.0,
        expiry="2026-09-05",
        ask=0.81,
        right="P",
        dte_bucket="0dte",
        exit_plan="TP +80% / SL -50%",
    )


def _buy_now_call(symbol: str = "NVDA") -> ActionSignal:
    return ActionSignal(
        action="BUY_NOW",
        symbol=symbol,
        strength=82.0,
        headline=f"BUY NOW · {symbol} call",
        detail=f"0DTE call sleeve on {symbol}",
        strike=120.0,
        expiry="2026-09-05",
        ask=1.20,
        right="C",
        dte_bucket="0dte",
        exit_plan="TP +80% / SL -50%",
    )


def test_diff_ladders_volume_oi_delta():
    prev = {
        "calls": [{"expiry": "2026-09-05", "strike": 100, "volume": 1000, "open_interest": 500, "mid": 1.0}],
        "puts": [],
    }
    curr = {
        "calls": [{"expiry": "2026-09-05", "strike": 100, "volume": 1500, "open_interest": 600, "mid": 1.0}],
        "puts": [],
    }
    deltas = diff_ladders(prev, curr)
    assert len(deltas) == 1
    assert deltas[0]["delta_volume"] == 500
    assert deltas[0]["delta_oi"] == 100
    assert deltas[0]["premium_delta"] == 50000.0


def test_attach_deltas_merges_into_ladder():
    prev = _bullish_ladder()["calls"][0]
    prev_ladder = {"calls": [{**prev, "volume": 14000, "open_interest": 4500}], "puts": []}
    curr = _bullish_ladder()
    merged = attach_deltas_to_ladder(curr, prev=prev_ladder)
    row = merged["calls"][0]
    assert row["delta_volume"] == 8000
    assert row["delta_oi"] == 500


def test_build_flow_leaders_ranks_bullish_and_bearish():
    flow = build_option_flow([_bullish_ladder(), _bearish_ladder()], min_volume=100, min_premium=1000)
    leaders = build_flow_leaders(flow["prints"], top_n=5)
    by_sym = {r["symbol"]: r for r in leaders}
    assert by_sym["NVDA"]["sentiment"] == "bullish"
    assert by_sym["NVDA"]["net_flow_score"] > 8
    assert by_sym["AMZN"]["sentiment"] == "bearish"
    assert by_sym["AMZN"]["net_flow_score"] < -8
    assert by_sym["NVDA"]["rank"] == 1 or by_sym["AMZN"]["rank"] == 1


def test_apply_flow_gate_blocks_put_into_bullish_rip():
    flow = build_option_flow([_bullish_ladder("AMZN", spot=180), _bearish_ladder("AMZN")], min_volume=100, min_premium=1000)
    # Simulate wrong-side put: AMZN shows up bullish from call-heavy tape
    leaders = build_flow_leaders(flow["prints"], top_n=12)
    # Force AMZN bullish leader to mimic rip + bearish put candidate mismatch
    amzn = next(r for r in leaders if r["symbol"] == "AMZN")
    amzn["sentiment"] = "bullish"
    amzn["net_flow_score"] = 25.0

    sig = apply_flow_gate(
        _buy_now_put("AMZN"),
        flow_leaders=leaders,
        require_flow_confirm=True,
    )
    assert sig.action == "WAIT"
    assert "blocked" in sig.detail.lower()
    assert "bullish" in sig.detail.lower()


def test_apply_flow_gate_allows_aligned_put():
    flow = build_option_flow([_bearish_ladder("AMZN")], min_volume=100, min_premium=1000)
    leaders = build_flow_leaders(flow["prints"], top_n=12)
    sig = apply_flow_gate(
        _buy_now_put("AMZN"),
        flow_leaders=leaders,
        require_flow_confirm=True,
    )
    assert sig.action == "BUY_NOW"
    assert "flow OK" in sig.detail


def test_apply_flow_gate_allows_aligned_call():
    flow = build_option_flow([_bullish_ladder("NVDA")], min_volume=100, min_premium=1000)
    leaders = build_flow_leaders(flow["prints"], top_n=12)
    sig = apply_flow_gate(
        _buy_now_call("NVDA"),
        flow_leaders=leaders,
        require_flow_confirm=True,
    )
    assert sig.action == "BUY_NOW"
    assert "flow OK" in sig.detail


def test_build_action_board_flow_gate_demotes_misaligned_put():
    flow = build_option_flow([_bullish_ladder("AMZN", spot=180)], min_volume=100, min_premium=1000)
    leaders = build_flow_leaders(flow["prints"], top_n=12)
    for row in leaders:
        if row["symbol"] == "AMZN":
            row["sentiment"] = "bullish"
            row["net_flow_score"] = 30.0

    board = build_action_board(
        candidates=[
            {
                "symbol": "AMZN",
                "score": 75,
                "right": "P",
                "strike": 175,
                "expiry": "2026-09-05",
                "ask": 0.81,
                "dte": 0,
                "dte_bucket": "0dte",
                "contract": "AMZN260905P00175000",
            }
        ],
        scores=[{"symbol": "AMZN", "ensemble_score": 40}],
        quotes={"AMZN": {"last": 180, "session_change_pct": 2.5}},
        ledger=None,
        require_hist_win=False,
        require_live_confirm=False,
        flow_leaders=leaders,
        require_flow_confirm=True,
    )
    assert board["counts"]["buy_now"] == 0
    assert board["counts"]["wait"] >= 1
    assert board["flow_gate"]["require"] is True
    assert len(board["flow_leaders"]) >= 1


def test_apply_flow_gate_missing_leader_soft_pass():
    sig = apply_flow_gate(
        _buy_now_call("ZZZZ"),
        flow_leaders=[{"symbol": "NVDA", "rank": 1, "sentiment": "bullish", "net_flow_score": 20, "top_tier": "golden"}],
        require_flow_confirm=True,
    )
    assert sig.action == "BUY_NOW"
    assert "flow n/a" in sig.detail.lower()


def test_journal_skips_enter_on_hard_flow_block(tmp_path):
    j = SignalJournal(tmp_path / "j.json", starting_cash=5000)
    blocked = {
        "action": "BUY_NOW",
        "symbol": "AMZN",
        "contract": "AMZN260905P00175000",
        "expiry": "2026-09-05",
        "strike": 175,
        "ask": 0.81,
        "score": 72,
        "dte_bucket": "0dte",
        "detail": "buy AMZN put · blocked: put vs bullish flow (got bullish, net +25)",
        "right": "P",
        "headline": "BUY NOW · AMZN put",
        "exit_plan": "TP +80%",
    }
    assert j.enter_from_signal(blocked, require_flow_gate=True) is None

    soft = dict(blocked)
    soft["detail"] = "buy AMZN call · flow n/a: AMZN not in top-12 leaders (tape/hist still gate)"
    soft["right"] = "C"
    soft["contract"] = "AMZN260905C00175000"
    t = j.enter_from_signal(soft, require_flow_gate=True)
    assert t is not None
    assert t.symbol == "AMZN"


def test_flow_leaders_from_cache(tmp_path, monkeypatch):
    from odte_scanner.echo import flow_deltas as fd
    from odte_scanner.echo.flow_snapshot import flow_leaders_from_cache

    import time

    cache = tmp_path / "echo_ladders"
    cache.mkdir()
    ladder = _bullish_ladder("SMCI")
    payload = {"_cached_at": time.time(), "ladder": ladder}
    (cache / "SMCI.json").write_text(json.dumps(payload))
    monkeypatch.setattr(fd, "CACHE_DIR", cache)

    leaders = flow_leaders_from_cache(top_n=5)
    assert len(leaders) == 1
    assert leaders[0]["symbol"] == "SMCI"
    assert leaders[0]["sentiment"] == "bullish"


def test_backtest_put_blocked_vs_allowed_counts():
    """Regression: flow gate should block more puts on bullish tape than bearish tape."""
    bullish_leaders = build_flow_leaders(
        build_option_flow([_bullish_ladder("AMZN", spot=180)], min_volume=100, min_premium=1000)["prints"],
        top_n=12,
    )
    bearish_leaders = build_flow_leaders(
        build_option_flow([_bearish_ladder("AMZN")], min_volume=100, min_premium=1000)["prints"],
        top_n=12,
    )
    put_sig_a = _buy_now_put("AMZN")
    put_sig_b = _buy_now_put("AMZN")
    blocked = apply_flow_gate(put_sig_a, flow_leaders=bullish_leaders, require_flow_confirm=True)
    allowed = apply_flow_gate(put_sig_b, flow_leaders=bearish_leaders, require_flow_confirm=True)
    assert blocked.action == "WAIT"
    assert allowed.action == "BUY_NOW"
