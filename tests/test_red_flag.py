"""Tests for VolSignals-inspired Red Flag proxy."""

from __future__ import annotations

from odte_scanner.signals.red_flag import (
    BOTTOM_LINE_RULES,
    apply_red_flag_to_actions,
    analyze_red_flag,
)


def test_bottom_line_rules_present():
    keys = {r["key"] for r in BOTTOM_LINE_RULES}
    assert keys == {"frmi", "tssi", "iren"}


def test_analyze_red_flag_returns_shape():
    rf = analyze_red_flag("SPY")
    assert rf["symbol"] == "SPY"
    assert rf["state"] in {"RED_FLAG", "NEUTRAL", "SUPPORTIVE"}
    assert 0 <= rf["score"] <= 100
    assert "reasons" in rf
    assert rf["proxy"] is True


def test_apply_red_flag_blocks_spy_0dte_buy():
    actions = {
        "primary": {
            "action": "BUY_NOW",
            "symbol": "SPY",
            "dte_bucket": "0dte",
            "headline": "BUY NOW SPY",
            "detail": "test",
            "strength": 80,
        },
        "all": [],
        "buy_now": [],
        "buy_now_0dte": [],
        "counts": {},
    }
    rf = {"block_0dte_long_calls": True, "state": "RED_FLAG", "symbol": "SPY", "score": 75}
    out = apply_red_flag_to_actions(actions, rf)
    assert out["primary"]["action"] == "WAIT"
    assert out["red_flag"]["active"] is True


def test_apply_red_flag_skips_when_inactive():
    actions = {"primary": {"action": "BUY_NOW", "symbol": "NVDA"}, "counts": {}}
    out = apply_red_flag_to_actions(actions, {"block_0dte_long_calls": False})
    assert out["primary"]["action"] == "BUY_NOW"
