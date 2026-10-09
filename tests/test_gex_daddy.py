"""Gex Daddy client + CALL BUY NOW wall gate."""

from __future__ import annotations

from odte_scanner.signals.actions import apply_gex_wall_gate
from odte_scanner.signals.gex_daddy import (
    call_into_wall,
    fetch_gex_daddy,
    short_gamma_above_flip,
)


def test_call_into_wall_and_short_gamma_helpers():
    assert call_into_wall(spot=779.5, call_wall=780.0, buffer_pct=0.15)
    assert call_into_wall(spot=780.5, call_wall=780.0, buffer_pct=0.15)
    assert not call_into_wall(spot=770.0, call_wall=780.0, buffer_pct=0.15)
    assert short_gamma_above_flip(spot=776.0, flip=774.9, gex_bias="negative")
    assert not short_gamma_above_flip(spot=770.0, flip=774.9, gex_bias="negative")
    assert not short_gamma_above_flip(spot=776.0, flip=774.9, gex_bias="positive")


def test_apply_gex_wall_gate_blocks_call_into_wall():
    board = {
        "all": [
            {
                "action": "BUY_NOW",
                "symbol": "SPY",
                "right": "C",
                "dte_bucket": "0dte",
                "live_last": 779.8,
                "strength": 80,
                "headline": "BUY NOW SPY CALL · 0DTE",
                "detail": "ensemble ok",
            }
        ],
        "buy_now": [],
        "wait": [],
        "counts": {},
    }
    board["buy_now"] = list(board["all"])
    walls = {
        "SPY": {
            "call_wall": 780.0,
            "put_wall": 767.0,
            "flip": 774.9,
            "gex_bias": "negative",
            "regime": "SHORT_GAMMA",
            "spot": 779.8,
            "source": "gex_daddy",
        }
    }
    out = apply_gex_wall_gate(board, walls, buffer_pct=0.15)
    assert out["all"][0]["action"] == "WAIT"
    assert out["all"][0].get("gex_blocked") is True
    assert out["counts"]["buy_now"] == 0
    assert "call wall" in (out["all"][0].get("detail") or "").lower()


def test_apply_gex_wall_gate_blocks_short_gamma_above_flip_index():
    board = {
        "all": [
            {
                "action": "BUY_NOW",
                "symbol": "SPY",
                "right": "C",
                "dte_bucket": "0dte",
                "live_last": 776.5,
                "strength": 82,
                "headline": "BUY NOW SPY CALL · 0DTE",
                "detail": "tape ok",
            }
        ],
        "buy_now": [],
        "wait": [],
        "counts": {},
    }
    board["buy_now"] = list(board["all"])
    walls = {
        "SPY": {
            "call_wall": 790.0,  # not into wall
            "put_wall": 760.0,
            "flip": 774.9,
            "gex_bias": "negative",
            "spot": 776.5,
            "source": "gex_daddy",
        }
    }
    out = apply_gex_wall_gate(board, walls)
    assert out["all"][0]["action"] == "WAIT"
    assert "short-gamma" in (out["all"][0].get("gex_block_reason") or "").lower()


def test_fetch_gex_daddy_live_smoke():
    """Live smoke — skip-soft if Render is cold/down."""
    row = fetch_gex_daddy("SPY", timeout=25.0)
    if not row.get("ok"):
        # Cold starts / network flakes should not fail CI hard
        assert "error" in row
        return
    assert row["ticker"] == "SPY"
    assert row.get("call_wall") is not None or row.get("put_wall") is not None
    assert row.get("source") == "gex_daddy"
