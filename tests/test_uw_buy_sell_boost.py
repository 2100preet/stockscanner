"""UW buy/sell boost on action signals."""

from __future__ import annotations

from odte_scanner.signals.actions import ActionSignal
from odte_scanner.signals.flow_gate import apply_uw_buy_boost, apply_uw_sell_boost


def _sig(**kwargs) -> ActionSignal:
    base = dict(
        symbol="NVDA",
        right="C",
        action="HOLD",
        headline="HOLD NVDA",
        detail="open",
        strength=60.0,
        dte_bucket="weekly",
    )
    base.update(kwargs)
    return ActionSignal(**base)


def test_uw_sell_boost_call_on_bearish():
    leaders = [
        {
            "symbol": "NVDA",
            "sentiment": "bearish",
            "net_flow_score": -120.0,
            "put_premium": 500000,
            "call_premium": 10000,
            "source": "unusual_whales",
            "rank": 1,
        }
    ]
    out = apply_uw_sell_boost(_sig(action="HOLD"), flow_leaders=leaders)
    assert out.action == "SELL_NOW"
    assert "UW exit" in out.detail


def test_uw_buy_boost_call_on_bullish():
    leaders = [
        {
            "symbol": "NVDA",
            "sentiment": "bullish",
            "net_flow_score": 200.0,
            "call_premium": 800000,
            "put_premium": 10000,
            "source": "unusual_whales",
            "rank": 1,
        }
    ]
    out = apply_uw_buy_boost(
        _sig(action="BUY_NOW", headline="BUY NOW NVDA", strength=70.0),
        flow_leaders=leaders,
    )
    assert out.action == "BUY_NOW"
    assert out.strength >= 78.0
    assert "UW BUY confirm" in out.detail
