"""Tests for chase / high-convexity alert lane."""
from __future__ import annotations

from odte_scanner.signals.chase_radar import build_chase_board, decide_chase_entry


def _ticket(**kwargs):
    base = {
        "symbol": "MU",
        "contract": "MU260814C00980000",
        "expiry": "2026-08-14",
        "dte": 1,
        "strike": 980,
        "spot": 969,
        "ask": 2.5,
        "bid": 2.3,
        "moneyness_pct": 1.13,
        "lottery_score": 70,
        "mult_at_1pct": 2.0,
        "mult_at_2pct": 3.5,
        "mult_at_3pct": 6.0,
        "mult_at_5pct": 12.0,
        "best_mult": 12.0,
        "volume": 5000,
        "open_interest": 1500,
        "score": 64,
    }
    base.update(kwargs)
    return base


def test_buy_risky_on_extended_rip_with_impulse():
    sig = decide_chase_entry(
        _ticket(ask=2.5, moneyness_pct=3.2),
        quote={"last": 969, "session_change_pct": 4.5, "mom_5m_pct": 0.15, "mom_15m_pct": 0.2},
        ensemble_score=64,
    )
    assert sig.action == "BUY_RISKY"
    assert "BIT RISKY" in sig.headline
    assert sig.risk_tag in {"chase", "extended", "far_otm", "richer_ask"}


def test_far_otm_watch_without_tape():
    sig = decide_chase_entry(
        _ticket(ask=1.2, moneyness_pct=5.0, mult_at_3pct=8.0, best_mult=15.0),
        quote={"last": 920, "session_change_pct": 0.2, "mom_5m_pct": 0.0},
        ensemble_score=60,
    )
    assert sig.action in {"WATCH_CONVEX", "BUY_RISKY", "CHASE_COOL"}
    # Without impulse, should not be cool if convexity strong — prefer WATCH
    assert sig.action == "WATCH_CONVEX"


def test_rich_ask_veto():
    sig = decide_chase_entry(
        _ticket(ask=18.0),
        quote={"last": 969, "session_change_pct": 5.0, "mom_5m_pct": 0.2},
        max_ask=12.0,
    )
    assert sig.action == "CHASE_COOL"
    assert any("ceiling" in v for v in sig.vetoes)


def test_knife_still_vetoed():
    sig = decide_chase_entry(
        _ticket(),
        quote={"last": 969, "session_change_pct": 3.0, "mom_5m_pct": -0.35},
    )
    assert sig.action == "CHASE_COOL"
    assert any("dump" in v for v in sig.vetoes)


def test_board_partitions():
    tickets = [
        _ticket(symbol="MU", ask=2.0, moneyness_pct=3.0),
        _ticket(symbol="NVDA", ask=0.8, moneyness_pct=4.0, mult_at_3pct=7.0),
    ]
    quotes = {
        "MU": {"last": 970, "session_change_pct": 3.5, "mom_5m_pct": 0.12},
        "NVDA": {"last": 130, "session_change_pct": 0.3, "mom_5m_pct": 0.0},
    }
    board = build_chase_board(tickets, quotes=quotes, scores=[{"symbol": "MU", "ensemble_score": 64}])
    assert "buy_risky" in board
    assert "watch" in board
    assert board["counts"]["all"] == 2
    assert "hist-win" in board["note"].lower() or "BUY NOW" in board["note"]
    assert "ensemble" in board["score_note"].lower() or "0DTE" in board["score_note"]
