"""Tests for META-class RIP / CONTINUATION lane + cooldown override."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import decide_entry
from odte_scanner.signals.rip_radar import (
    build_rip_board,
    decide_rip_entry,
    is_mega_rip_symbol,
    mega_rip_tape_ok,
)

_MORNING = datetime(2026, 9, 18, 11, 0, tzinfo=ZoneInfo("America/New_York"))


def test_mega_symbols_include_baba_googl_amd_meta():
    for s in ("BABA", "GOOGL", "AMD", "META", "NVDA"):
        assert is_mega_rip_symbol(s)


def test_mega_rip_tape_ok():
    assert mega_rip_tape_ok(live=1.4, mom5=0.12)
    assert not mega_rip_tape_ok(live=0.4, mom5=0.12)
    assert not mega_rip_tape_ok(live=1.4, mom5=-0.1)


def test_buy_rip_on_meta_class_continuation():
    act = decide_rip_entry(
        {
            "symbol": "META",
            "ask": 4.5,
            "bid": 4.2,
            "strike": 760,
            "expiry": "2026-09-25",
            "contract": "META260925C00760000",
            "dte": 4,
            "dte_bucket": "weekly",
            "moneyness_pct": 1.2,
            "volume": 800,
            "open_interest": 2000,
            "score": 70,
        },
        quote={"last": 752, "session_change_pct": 2.1, "mom_5m_pct": 0.18, "mom_15m_pct": 0.4},
        now=_MORNING,
    )
    assert act.action == "BUY_RIP"
    assert act.cooldown_waived is True


def test_same_occ_still_blocked_on_rip_lane():
    act = decide_rip_entry(
        {
            "symbol": "META",
            "ask": 4.5,
            "bid": 4.2,
            "strike": 682.5,
            "expiry": "2026-09-21",
            "contract": "META260921C00682500",
            "dte": 2,
            "volume": 500,
            "score": 70,
        },
        quote={"last": 700, "session_change_pct": 3.0, "mom_5m_pct": 0.2},
        loss_cooldown_contracts={"META260921C00682500"},
        now=_MORNING,
    )
    assert act.action == "RIP_COOL"
    assert "cooldown" in act.detail.lower()


def test_options_symbol_cooldown_waived_when_mega_ripping():
    """Regression: META melt-up must not stay WAIT solely for symbol cooldown."""
    cand = {
        "symbol": "META",
        "score": 70,
        "strike": 760,
        "expiry": "2026-09-25",
        "ask": 5.0,
        "bid": 4.8,
        "contract": "META260925C00760000",  # fresh OCC — not the loser
        "dte": 4,
        "dte_bucket": "weekly",
    }
    blocked = decide_entry(
        cand,
        quote={"last": 752, "session_change_pct": 0.2, "mom_5m_pct": 0.05},
        buy_score=65,
        weekly_buy_score=65,
        require_live_confirm=False,
        now=_MORNING,
        loss_cooldown_symbols={"META"},
        loss_cooldown_contracts=set(),
    )
    assert blocked.action == "WAIT"
    assert "cooldown" in blocked.detail.lower()

    ripping = decide_entry(
        cand,
        quote={"last": 752, "session_change_pct": 2.2, "mom_5m_pct": 0.15, "mom_15m_pct": 0.3},
        buy_score=65,
        weekly_buy_score=65,
        require_live_confirm=False,
        now=_MORNING,
        loss_cooldown_symbols={"META"},
        loss_cooldown_contracts=set(),
    )
    # May still WAIT for other gates, but not symbol cooldown
    assert "on loss cooldown — recent losing flip" not in ripping.detail


def test_rip_board_includes_baba_watch_without_chain():
    board = build_rip_board(
        candidates=[{"symbol": "BABA", "score": 68, "right": "C"}],
        scores=[{"symbol": "BABA", "ensemble_score": 68}],
        quotes={"BABA": {"last": 120, "session_change_pct": 1.8, "mom_5m_pct": 0.2}},
        now=_MORNING,
    )
    syms = [r["symbol"] for r in (board.get("watch") or []) + (board.get("buy_rip") or [])]
    assert "BABA" in syms or any(r.get("symbol") == "BABA" for r in board.get("all") or [])
