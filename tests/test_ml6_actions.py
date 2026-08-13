"""Tests for ML6 BUY NOW / SELL NOW automation."""

from __future__ import annotations

from odte_scanner.ml6.actions import (
    build_ml6_action_board,
    decide_ml6_entry,
    decide_ml6_exit,
    reaction_accepted,
)


def test_pre_print_blocks_buy():
    row = {
        "symbol": "IREN",
        "ensemble_score": 85,
        "status": "WATCH",
        "days_to_earnings": 14,
        "liquidity_ok": True,
    }
    sig = decide_ml6_entry(row, quote={"last": 40, "session_change_pct": 5}, attach_call=False)
    assert sig.action == "WATCH"
    assert "pre-print" in (sig.detail or "").lower() or "pre_print" in (sig.reasons or [])


def test_reaction_accept_strong_session():
    row = {"symbol": "FRMI", "days_to_earnings": 0, "accepted": False}
    ok, why = reaction_accepted(
        row,
        {"last": 7, "session_change_pct": 3.2, "mom_5m_pct": 0.4, "dist_from_day_high_pct": -0.2},
    )
    assert ok is True
    assert why


def test_buy_now_when_accepted_without_call_stays_wait():
    row = {
        "symbol": "TSSI",
        "ensemble_score": 80,
        "status": "BUY_ONLY_IF_ACCEPTED",
        "days_to_earnings": 0,
        "liquidity_ok": True,
        "accepted": True,
        "last_price": 12.5,
    }
    sig = decide_ml6_entry(
        row,
        quote={"last": 12.5, "session_change_pct": 4.0, "mom_5m_pct": 0.5},
        attach_call=False,
    )
    assert sig.action == "WAIT"
    assert sig.accepted is True


def test_sell_now_on_stop():
    trade = {
        "status": "open",
        "symbol": "FRMI",
        "entry_ask": 1.0,
        "mark": 0.5,
        "entry_reason": "BUY NOW ML6 FRMI",
        "contract": "FRMI260820C00007000",
        "desk": "ml6",
    }
    sig = decide_ml6_exit(trade, quote={"session_change_pct": -1}, stop_loss_pct=40)
    assert sig is not None
    assert sig.action == "SELL_NOW"


def test_action_board_counts():
    watchlist = [
        {
            "symbol": "IREN",
            "ensemble_score": 80,
            "status": "WATCH",
            "days_to_earnings": 10,
            "liquidity_ok": True,
        }
    ]
    board = build_ml6_action_board(watchlist, quotes={}, attach_calls=False)
    assert board["counts"]["watch"] >= 1
    assert board["counts"]["buy_now"] == 0
