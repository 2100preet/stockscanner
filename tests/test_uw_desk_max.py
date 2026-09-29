"""Desk-wide Unusual Whales annotation coverage."""

from __future__ import annotations

from odte_scanner.signals.flow_gate import annotate_dict_with_uw, apply_uw_market_tide
from odte_scanner.signals.actions import ActionSignal
from odte_scanner.signals.unusual_whales import fetch_market_tide


def test_annotate_dict_vetoes_call_on_bearish_uw():
    leaders = [
        {
            "symbol": "NVDA",
            "sentiment": "bearish",
            "net_flow_score": -150,
            "source": "unusual_whales",
            "rank": 1,
        }
    ]
    row = annotate_dict_with_uw(
        {"symbol": "NVDA", "action": "BUY_RIP", "detail": "rip", "strength": 80, "right": "C"},
        flow_leaders=leaders,
        hard_block=True,
    )
    assert row["action"] == "WATCH_RIP"
    assert "UW veto" in row["detail"]


def test_annotate_dict_confirms_put():
    leaders = [
        {
            "symbol": "SPY",
            "sentiment": "bearish",
            "net_flow_score": -200,
            "source": "unusual_whales",
            "rank": 1,
        }
    ]
    row = annotate_dict_with_uw(
        {"symbol": "SPY", "action": "PUT_NOW", "detail": "orb", "strength": 70, "right": "P"},
        flow_leaders=leaders,
        right_default="P",
        hard_block=True,
    )
    assert row["action"] == "PUT_NOW"
    assert "UW PUT confirm" in row["detail"]
    assert row["strength"] >= 78


def test_market_tide_soft_haircut_on_call_buy():
    sig = ActionSignal(
        action="BUY_NOW",
        symbol="AMD",
        strength=75,
        headline="BUY NOW AMD",
        detail="setup",
        right="C",
    )
    out = apply_uw_market_tide(
        sig,
        market_tide={"ok": True, "sentiment": "bearish", "tide_net": -400_000_000},
    )
    assert out.action == "BUY_NOW"
    assert "UW tide bearish" in out.detail
    assert out.strength <= 65


def test_fetch_market_tide_skipped_without_key(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    out = fetch_market_tide()
    assert out.get("skipped") is True
