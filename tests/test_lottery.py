"""Lottery BUY NOW / SELL NOW playbook gates."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery import (
    build_lottery_board,
    decide_lottery_entry,
    decide_lottery_exit,
)

ET = ZoneInfo("America/New_York")
# Mid-session weekday so session timing confirms fire
SESSION_NOW = datetime(2026, 8, 5, 11, 30, tzinfo=ET)


def _ticket(**overrides):
    base = {
        "symbol": "SPY",
        "contract": "SPY260805C00650000",
        "expiry": "2026-08-05",
        "dte": 0,
        "strike": 650,
        "spot": 648,
        "ask": 2.40,
        "bid": 2.30,
        "moneyness_pct": 0.3,
        "volume": 1200,
        "open_interest": 8000,
        "score": 74,
        "lottery_score": 78,
        "mult_at_2pct": 4.5,
        "mult_at_3pct": 8.0,
        "mult_at_5pct": 18.0,
        "best_mult": 18.0,
        "pct_gain_best": 1700,
    }
    base.update(overrides)
    return base


def _quote(**overrides):
    base = {
        "last": 649.5,
        "session_change_pct": 0.55,
        "change_pct": 0.55,
        "mom_5m_pct": 0.18,
        "mom_15m_pct": 0.22,
        "dist_from_day_high_pct": -0.12,
    }
    base.update(overrides)
    return base


def test_buy_now_when_convexity_and_tape_clear():
    sig = decide_lottery_entry(
        _ticket(),
        quote=_quote(),
        ensemble_score=74,
        now=SESSION_NOW,
    )
    assert sig.action == "BUY_NOW"
    assert "asymmetric_payoff" in sig.playbook
    assert "tape_confirm" in sig.playbook
    assert sig.confirms >= 4


def test_wait_without_tape_even_if_convex():
    sig = decide_lottery_entry(
        _ticket(),
        quote=_quote(mom_5m_pct=0.01, mom_15m_pct=0.02, session_change_pct=0.05, change_pct=0.05),
        ensemble_score=74,
        now=SESSION_NOW,
    )
    assert sig.action == "WAIT"
    assert "missing_tape_confirm" in sig.vetoes


def test_skip_on_knife_dump():
    sig = decide_lottery_entry(
        _ticket(),
        quote=_quote(mom_5m_pct=-0.3, mom_15m_pct=-0.4, session_change_pct=-1.1),
        ensemble_score=74,
        now=SESSION_NOW,
    )
    assert sig.action == "SKIP"
    assert any("knife" in v.lower() or "dump" in v.lower() for v in sig.vetoes)


def test_skip_weak_convexity():
    sig = decide_lottery_entry(
        _ticket(mult_at_3pct=1.5, mult_at_5pct=2.0, best_mult=2.0, lottery_score=70),
        quote=_quote(),
        ensemble_score=74,
        now=SESSION_NOW,
    )
    assert sig.action == "SKIP"
    assert any("convexity" in v.lower() for v in sig.vetoes)


def test_skip_final_30_no_new_lottery():
    late = datetime(2026, 8, 5, 15, 45, tzinfo=ET)
    sig = decide_lottery_entry(
        _ticket(),
        quote=_quote(),
        ensemble_score=74,
        now=late,
    )
    assert sig.action == "SKIP"
    assert any("final_30" in v for v in sig.vetoes)


def test_sell_now_on_take_profit():
    sig = decide_lottery_exit(
        {
            "status": "open",
            "symbol": "SPY",
            "contract": "SPY260805C00650000",
            "entry_ask": 2.0,
            "mark": 5.0,
            "dte_bucket": "0dte",
            "id": "t1",
        },
        quote=_quote(),
        mark=5.0,
        now=SESSION_NOW,
    )
    assert sig is not None
    assert sig.action == "SELL_NOW"
    assert sig.option_unrealized_pct is not None and sig.option_unrealized_pct >= 120


def test_sell_now_on_tape_fail():
    sig = decide_lottery_exit(
        {
            "status": "open",
            "symbol": "QQQ",
            "contract": "X",
            "entry_ask": 3.0,
            "mark": 2.8,
            "dte_bucket": "0dte",
            "id": "t2",
        },
        quote=_quote(mom_5m_pct=-0.35, mom_15m_pct=-0.5, session_change_pct=-0.2),
        mark=2.8,
        now=SESSION_NOW,
    )
    assert sig is not None
    assert sig.action == "SELL_NOW"
    assert "tape_fail" in sig.playbook


def test_board_primary_prefers_sell_over_buy():
    board = build_lottery_board(
        [_ticket()],
        quotes={"SPY": _quote()},
        scores=[{"symbol": "SPY", "horizon": "0dte", "ensemble_score": 76}],
        open_trades=[
            {
                "status": "open",
                "symbol": "SPY",
                "contract": "SPY260805C00650000",
                "entry_ask": 1.5,
                "mark": 6.0,
                "dte_bucket": "0dte",
                "id": "open1",
            }
        ],
        now=SESSION_NOW,
    )
    assert board["primary"] is not None
    assert board["primary"]["action"] == "SELL_NOW"
    assert board["counts"]["sell_now"] >= 1
    # Same contract already open → entry side HOLD, not another BUY
    assert board["counts"]["buy_now"] == 0


def test_board_does_not_apply_lottery_exit_to_weekly():
    board = build_lottery_board(
        [_ticket(symbol="NVDA", contract="NVDA_LOTTO")],
        quotes={"AAPL": _quote(), "NVDA": _quote()},
        scores=[{"symbol": "NVDA", "horizon": "0dte", "ensemble_score": 80}],
        open_trades=[
            {
                "status": "open",
                "symbol": "AAPL",
                "contract": "AAPL_WEEKLY",
                "entry_ask": 2.0,
                "mark": 1.0,
                "dte_bucket": "weekly",
                "id": "w1",
            }
        ],
        now=SESSION_NOW,
    )
    assert board["counts"]["sell_now"] == 0
    assert board["counts"]["hold"] == 0
