from odte_scanner.signals.actions import decide_entry
from odte_scanner.signals.hold_rules import exit_plan_text, premium_exit_levels


def test_premium_exit_levels_from_ask():
    tp, sl = premium_exit_levels(0.50, take_profit_pct=80, stop_loss_pct=50)
    assert tp == 0.90
    assert sl == 0.25


def test_exit_plan_text_includes_dollar_tp_sl():
    plan = exit_plan_text(
        dte_bucket="0dte",
        dte=0,
        right="C",
        take_profit_pct=80,
        stop_loss_pct=50,
        ask=0.35,
        soft_exit=540.0,
    )
    assert "TP ≥$0.63 (+80%)" in plan
    assert "SL ≤$0.17 (−50%)" in plan  # 0.35 * 0.5 → 0.175 → 0.17
    assert "soft wall spot ≥ $540.00" in plan
    # Not bare percent-only labels when ask is known
    assert plan.index("TP ≥$") >= 0


def test_decide_entry_attaches_target_and_stop_ask():
    sig = decide_entry(
        {
            "symbol": "MSFT",
            "score": 74,
            "right": "C",
            "strike": 540,
            "expiry": "2026-10-07",
            "ask": 0.35,
            "bid": 0.34,
            "contract": "MSFT251007C00540000",
            "dte": 0,
            "dte_bucket": "0dte",
            "soft_exit": 545.0,
        },
        quote={"session_change_pct": 0.4, "mom_5m_pct": 0.1, "last": 535.0},
        buy_score=70,
        wait_score=62,
        require_live_confirm=False,
        take_profit_pct=80,
        stop_loss_pct=50,
    )
    assert sig.target_ask == 0.63
    assert sig.stop_ask == 0.17
    assert "≥$0.63" in (sig.exit_plan or "")
    assert "≤$0.17" in (sig.exit_plan or "")
