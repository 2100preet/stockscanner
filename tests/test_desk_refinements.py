"""Nov 17 desk review: RIP market tailwind gate + Swing paper-journal guards."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import bank_move_ready
from odte_scanner.signals.hold_rules import time_stop_reason
from odte_scanner.signals.rip_radar import (
    decide_rip_entry,
    market_change_from_quotes,
    market_gate_block,
)
from odte_scanner.trading.journal import SignalJournal, closed_today, regular_session_open

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
_MORNING = datetime(2026, 9, 18, 11, 0, tzinfo=ET)

_TICKET = {
    "symbol": "AMD",
    "ask": 3.5,
    "bid": 3.3,
    "strike": 160,
    "expiry": "2026-09-25",
    "contract": "AMD260925C00160000",
    "dte": 5,
    "dte_bucket": "weekly",
    "moneyness_pct": 0.3,
    "volume": 900,
    "open_interest": 2000,
    "score": 48,
}
_HOT = {"last": 160.5, "session_change_pct": 2.4, "mom_5m_pct": 0.2, "mom_15m_pct": 0.4}


def test_market_gate_block_rules():
    assert market_gate_block(2.0, 0.2, min_market_pct=None) is None
    assert market_gate_block(2.0, 1.2, min_market_pct=1.0) is None
    assert "QQQ" in market_gate_block(2.0, 0.4, min_market_pct=1.0)
    assert "unknown" in market_gate_block(2.0, None, min_market_pct=1.0)
    assert "session" in market_gate_block(0.6, 1.5, min_market_pct=1.0)


def test_market_change_from_quotes():
    assert market_change_from_quotes({"QQQ": {"session_change_pct": 1.3}}) == 1.3
    assert market_change_from_quotes({}) is None


def test_rip_buy_needs_qqq_tailwind_when_gate_on():
    off = decide_rip_entry(dict(_TICKET), quote=dict(_HOT), now=_MORNING)
    assert off.action == "BUY_RIP"
    weak = decide_rip_entry(
        dict(_TICKET), quote=dict(_HOT), now=_MORNING, market_change_pct=0.3, min_market_pct=1.0
    )
    assert weak.action == "WATCH_RIP"
    assert "QQQ" in weak.detail
    strong = decide_rip_entry(
        dict(_TICKET), quote=dict(_HOT), now=_MORNING, market_change_pct=1.4, min_market_pct=1.0
    )
    assert strong.action == "BUY_RIP"


def test_bank_exit_waits_for_move_after_late_entry():
    # Entered flat on the day, now +2% → bank (old behaviour kept).
    assert bank_move_ready(2.0, 102.0, 100.0)
    # Entered already +2.5% (pullback reclaim), now +2.6% → hold.
    assert not bank_move_ready(2.6, 102.6, 102.5)
    # ...then a further +1.8% from entry → bank.
    assert bank_move_ready(4.4, 104.4, 102.5)
    # No entry spot recorded → old session rule.
    assert bank_move_ready(2.0, 102.0, None)
    # Puts mirror.
    assert not bank_move_ready(-2.6, 97.4, 97.5, put=True)
    assert bank_move_ready(-2.0, 98.0, 100.0, put=True)
    assert not bank_move_ready(1.0, 101.0, 100.0)


def test_regular_session_open_ct():
    assert regular_session_open(datetime(2026, 11, 17, 9, 0, tzinfo=CT))
    assert not regular_session_open(datetime(2026, 11, 17, 7, 0, tzinfo=CT))
    assert not regular_session_open(datetime(2026, 11, 17, 21, 0, tzinfo=CT))
    assert not regular_session_open(datetime(2026, 11, 21, 10, 0, tzinfo=CT))  # Saturday


def _signal():
    return {
        "action": "BUY_NOW",
        "symbol": "XOM",
        "contract": "XOM261127C00120000",
        "expiry": "2026-11-27",
        "strike": 120,
        "ask": 1.0,
        "bid": 0.95,
        "score": 75,
        "dte_bucket": "0dte",
        "detail": "[1W] test",
        "live_last": 119.5,
    }


def test_journal_rth_only_blocks_night_entries(tmp_path):
    j = SignalJournal(tmp_path / "j.json", starting_cash=5000)
    night = datetime(2026, 11, 17, 20, 0, tzinfo=CT)
    assert j.enter_from_signal(_signal(), now=night, rth_only=True) is None
    # Default (tests / other callers) unchanged.
    assert j.enter_from_signal(_signal(), now=night) is not None


def test_journal_blocks_same_day_reentry_of_closed_contract(tmp_path):
    j = SignalJournal(tmp_path / "j.json", starting_cash=5000)
    morning = datetime(2026, 11, 17, 9, 0, tzinfo=CT)
    t = j.enter_from_signal(_signal(), now=morning, rth_only=True)
    assert t is not None
    t.status = "closed"
    t.exited_at = (morning + timedelta(minutes=5)).isoformat()
    assert closed_today(j.book.trades, t.contract, morning + timedelta(minutes=10))
    assert j.enter_from_signal(_signal(), now=morning + timedelta(minutes=10), rth_only=True) is None
    nxt = datetime(2026, 11, 18, 9, 0, tzinfo=CT)
    assert j.enter_from_signal(_signal(), now=nxt, rth_only=True) is not None


def test_weekly_contract_labelled_0dte_skips_0dte_flatten():
    late = datetime(2026, 11, 17, 15, 50, tzinfo=ET)
    trade = {
        "dte_bucket": "0dte",
        "expiry": "2026-11-27",
        "entered_at": (late - timedelta(minutes=30)).isoformat(),
        "status": "open",
    }
    assert time_stop_reason(trade, now=late) is None
    same_day = dict(trade, expiry="2026-11-17")
    assert time_stop_reason(same_day, now=late)
