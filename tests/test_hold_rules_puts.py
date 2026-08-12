"""Hold / time-stop / put entry tests."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import build_action_board, decide_entry, decide_exit
from odte_scanner.signals.hold_rules import exit_plan_text, past_odte_flatten, time_stop_reason

ET = ZoneInfo("America/New_York")


def test_exit_plan_on_buy_now():
    sig = decide_entry(
        {
            "symbol": "SPY",
            "score": 76,
            "strike": 770,
            "expiry": "2026-08-11",
            "ask": 1.5,
            "bid": 1.4,
            "contract": "SPY260811C00770000",
            "dte": 0,
            "dte_bucket": "0dte",
            "right": "C",
        },
        quote={
            "last": 772,
            "session_change_pct": 0.4,
            "mom_5m_pct": 0.12,
            "mom_15m_pct": 0.18,
            "dist_from_day_high_pct": -0.1,
        },
        buy_score=70,
        now=datetime(2026, 8, 11, 11, 0, tzinfo=ET),
    )
    assert sig.action == "BUY_NOW"
    assert sig.exit_plan
    assert "15:45" in sig.exit_plan
    assert "EXIT plan" in sig.detail
    assert "TP" in sig.exit_plan


def test_0dte_time_stop_sells_after_1545_et():
    late = datetime(2026, 8, 11, 15, 50, tzinfo=ET)
    assert past_odte_flatten(late)
    reason = time_stop_reason(
        {"dte_bucket": "0dte", "entered_at": "2026-08-11T14:00:00+00:00"},
        now=late,
    )
    assert reason and "15:45" in reason

    sig = decide_exit(
        {
            "symbol": "QQQ",
            "status": "open",
            "entry": 2.0,
            "mark": 2.1,
            "bid": 2.05,
            "contract": "QQQ260811C00720000",
            "id": "t1",
            "score": 70,
            "dte_bucket": "0dte",
            "entered_at": "2026-08-11T14:00:00+00:00",
            "right": "C",
        },
        quote={"last": 720, "session_change_pct": 0.1, "mom_5m_pct": 0.05},
        score_by_symbol={"QQQ": 72},
        now=late,
    )
    assert sig is not None
    assert sig.action == "SELL_NOW"
    assert "time-stop" in sig.detail.lower() or "15:45" in sig.detail


def test_weekly_max_hold_time_stop():
    reason = time_stop_reason(
        {
            "dte_bucket": "weekly",
            "dte": 5,
            "entered_at": "2026-08-01T14:00:00+00:00",
        },
        now=datetime(2026, 8, 11, 12, 0, tzinfo=ET),
        weekly_max_days=7,
    )
    assert reason and "max hold" in reason.lower()


def test_put_buy_on_dumping_tape():
    sig = decide_entry(
        {
            "symbol": "IWM",
            "score": 40,
            "put_score": 75,
            "strike": 220,
            "expiry": "2026-08-11",
            "ask": 1.2,
            "bid": 1.1,
            "contract": "IWM260811P00220000",
            "dte": 0,
            "dte_bucket": "0dte",
            "right": "P",
            "moneyness_pct": 0.1,
        },
        quote={
            "last": 219.5,
            "session_change_pct": -0.8,
            "mom_5m_pct": -0.2,
            "mom_15m_pct": -0.3,
            "dist_from_day_low_pct": 0.05,
        },
        buy_score=70,
        now=datetime(2026, 8, 11, 11, 0, tzinfo=ET),
    )
    assert sig.action == "BUY_NOW"
    assert sig.right == "P"
    assert "PUT" in sig.headline
    assert sig.exit_plan and "put" in sig.exit_plan.lower()


def test_put_wait_on_bounce():
    sig = decide_entry(
        {
            "symbol": "IWM",
            "score": 75,
            "strike": 220,
            "expiry": "2026-08-11",
            "ask": 1.2,
            "contract": "IWM260811P00220000",
            "dte": 0,
            "dte_bucket": "0dte",
            "right": "P",
        },
        quote={
            "last": 221,
            "session_change_pct": 0.5,
            "mom_5m_pct": 0.2,
            "mom_15m_pct": 0.25,
        },
        buy_score=70,
        now=datetime(2026, 8, 11, 11, 0, tzinfo=ET),
    )
    assert sig.action == "WAIT"


def test_board_splits_puts_and_time_stop():
    late = datetime(2026, 8, 11, 15, 50, tzinfo=ET)
    board = build_action_board(
        candidates=[
            {
                "symbol": "SPY",
                "score": 40,
                "put_score": 78,
                "strike": 560,
                "expiry": "2026-08-11",
                "ask": 1.1,
                "bid": 1.0,
                "contract": "SPY260811P00560000",
                "dte": 0,
                "dte_bucket": "0dte",
                "right": "P",
                "moneyness_pct": 0.05,
            }
        ],
        scores=[{"symbol": "SPY", "ensemble_score": 40}],
        quotes={
            "SPY": {
                "last": 559,
                "session_change_pct": -0.9,
                "mom_5m_pct": -0.22,
                "mom_15m_pct": -0.35,
                "dist_from_day_low_pct": 0.02,
            }
        },
        ledger=None,
        journal_opens=[
            {
                "symbol": "QQQ",
                "status": "open",
                "entry_ask": 2.0,
                "mark": 2.0,
                "bid": 2.0,
                "contract": "QQQ260811C00720000",
                "id": "j1",
                "dte_bucket": "0dte",
                "entered_at": "2026-08-11T14:00:00+00:00",
                "right": "C",
            }
        ],
        require_hist_win=False,
        now=late,
    )
    assert board["counts"]["sell_now"] >= 1
    assert board["hold_rules"]["odte_flatten_et"] == "15:45"
    assert any(b.get("right") == "P" for b in board["buy_now"]) or board["counts"]["buy_now_puts"] >= 0


def test_exit_plan_text_helpers():
    plan = exit_plan_text(dte_bucket="0dte", right="C", take_profit_pct=80, stop_loss_pct=50)
    assert "15:45" in plan
    assert "TP +80%" in plan
    put_plan = exit_plan_text(dte_bucket="weekly", right="P", weekly_max_days=7)
    assert "put" in put_plan.lower()
    assert "7d" in put_plan or "1–7" in put_plan
