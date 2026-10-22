"""Expired / same-day contracts must not stay BUY NOW on stale Pages snapshots."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import decide_entry
from odte_scanner.signals.hold_rules import contract_expired, expiry_is_today
from odte_scanner.trading.journal import SignalJournal

ET = ZoneInfo("America/New_York")


def test_contract_expired_helpers():
    now = datetime(2026, 9, 10, 11, 0, tzinfo=ET)
    assert contract_expired("2026-09-09", now) is True
    assert contract_expired("2026-09-10", now) is False
    assert expiry_is_today("2026-09-10", now) is True
    assert expiry_is_today("2026-09-09", now) is False


def test_decide_entry_blocks_expired_mu_even_offline():
    """Regression: MU 9/9 0DTE stayed BUY NOW on Sep 10 Pages snapshot."""
    now = datetime(2026, 9, 10, 11, 30, tzinfo=ET)
    sig = decide_entry(
        {
            "symbol": "MU",
            "score": 70,
            "right": "C",
            "strike": 1025,
            "expiry": "2026-09-09",
            "ask": 3.55,
            "dte": 0,
            "dte_bucket": "0dte",
            "contract": "MU260909C01025000",
        },
        quote={"last": 1030, "session_change_pct": 0.5, "mom_5m_pct": 0.12, "mom_15m_pct": 0.1},
        buy_score=62,
        require_live_confirm=False,  # Pages offline
        now=now,
    )
    assert sig.action == "WAIT"
    assert "expired" in sig.detail.lower()


def test_decide_entry_blocks_same_day_after_1500_offline():
    now = datetime(2026, 9, 9, 17, 36, tzinfo=ET)  # after close, expiry day
    sig = decide_entry(
        {
            "symbol": "MU",
            "score": 70,
            "right": "C",
            "strike": 1025,
            "expiry": "2026-09-09",
            "ask": 3.55,
            "dte": 0,
            "dte_bucket": "0dte",
            "contract": "MU260909C01025000",
        },
        quote={"last": 1030, "session_change_pct": 0.5, "mom_5m_pct": 0.12, "mom_15m_pct": 0.1},
        buy_score=62,
        require_live_confirm=False,
        now=now,
    )
    assert sig.action == "WAIT"
    assert "15:00" in sig.detail or "Past 15:00" in sig.detail


def _cost_candidate(**extra):
    row = {
        "symbol": "COST",
        "score": 75,
        "right": "C",
        "strike": 1025,
        "expiry": "2026-10-23",
        "ask": 4.2,
        "dte": 1,
        "dte_bucket": "0dte",
        "contract": "COST261023C01025000",
        "moneyness_pct": 1.8,
    }
    row.update(extra)
    return row


def test_missing_momentum_does_not_fail_otm_gate():
    """A green session with no 5m print is not 'tape flat/down'."""
    now = datetime(2026, 10, 22, 12, 48, tzinfo=ET)
    quote = {"session_change_pct": 1.7, "mom_5m_pct": None, "mom_15m_pct": None}
    missing = decide_entry(
        _cost_candidate(),
        quote=quote,
        buy_score=65,
        require_live_confirm=True,
        now=now,
    )
    assert "flat/down" not in missing.detail
    assert missing.action == "BUY_NOW"

    flat = decide_entry(
        _cost_candidate(),
        quote={**quote, "mom_5m_pct": 0.0},
        buy_score=65,
        require_live_confirm=True,
        now=now,
    )
    assert flat.action == "WAIT"
    assert "flat/down" in flat.detail


def test_journal_expiry_follows_session_clock(tmp_path):
    """Replay clock, not the wall clock, decides whether a contract is expired."""
    session = datetime(2026, 9, 9, 11, 0, tzinfo=ET)
    later = datetime(2026, 9, 10, 11, 0, tzinfo=ET)
    signal = {
        "action": "BUY_NOW",
        "symbol": "MU",
        "contract": "MU260909C01025000",
        "expiry": "2026-09-09",
        "strike": 1025,
        "ask": 1.00,
        "right": "C",
        "headline": "BUY NOW",
        "detail": "same-day ticket",
        "exit_plan": "TP",
    }
    opened = SignalJournal(tmp_path / "open.json", starting_cash=5000)
    trade = opened.enter_from_signal(signal, now=session)
    assert trade is not None
    assert trade.expiry == "2026-09-09"

    skipped = SignalJournal(tmp_path / "skip.json", starting_cash=5000)
    assert skipped.enter_from_signal(signal, now=later) is None


def test_journal_skips_expired_contract(tmp_path):
    j = SignalJournal(tmp_path / "j.json", starting_cash=5000)
    t = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "MU",
            "contract": "MU260909C01025000",
            "expiry": "2026-09-09",
            "strike": 1025,
            "ask": 3.55,
            "right": "C",
            "headline": "BUY NOW",
            "detail": "expired ticket",
            "exit_plan": "TP",
        }
    )
    assert t is None
