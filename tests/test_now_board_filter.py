"""BUY NOW must not repeat contracts already CLOSED in rec-log/journal."""

from __future__ import annotations

from odte_scanner.signals.now_board_filter import (
    apply_settled_contract_filter,
    settled_buy_contracts,
)
from odte_scanner.trading.journal import JournalTrade, SignalJournal, TradeJournal


def test_settled_buy_contracts_rec_closed_wins_over_stale_journal_open():
    """Options tab CLOSED must hide BUY even if insights still lists an open clone."""
    book = TradeJournal(
        starting_cash=1000,
        cash=800,
        trades=[
            JournalTrade(
                id="TSM-open",
                symbol="TSM",
                contract="TSM261002C00472500",
                expiry="2026-10-02",
                strike=472.5,
                dte_bucket="0dte",
                status="open",
                entry_ask=1.0,
                entry_score=70.0,
                entry_reason="test",
                entered_at="2026-10-02T12:00:00+00:00",
            ),
            JournalTrade(
                id="TSM-closed",
                symbol="TSM",
                contract="TSM261002C00472500",
                expiry="2026-10-02",
                strike=472.5,
                dte_bucket="0dte",
                status="closed",
                entry_ask=1.0,
                entry_score=70.0,
                entry_reason="test",
                entered_at="2026-10-02T11:00:00+00:00",
                exited_at="2026-10-02T11:30:00+00:00",
            ),
        ],
    )
    j = SignalJournal.__new__(SignalJournal)
    j.book = book
    rec = {
        "closed_recs": [
            {"symbol": "TSM", "contract": "TSM261002C00472500", "status": "closed"},
        ],
        "open_recs": [],
    }
    settled = settled_buy_contracts(journal=j, rec_log=rec)
    assert "TSM261002C00472500" in settled


def test_apply_filter_strips_lottery_and_rip_buys():
    settled = {"TSM261002C00472500"}
    lottery = {
        "buy_now": [
            {"symbol": "TSM", "contract": "TSM261002C00472500", "action": "BUY_NOW"},
            {"symbol": "NVDA", "contract": "NVDA261002C00180000", "action": "BUY_NOW"},
        ],
        "counts": {"buy_now": 2},
    }
    rip = {
        "buy_rip": [
            {"symbol": "TSM", "contract": "TSM261002C00472500", "action": "BUY_RIP"},
        ],
        "counts": {"buy_rip": 1, "buy_now": 1},
    }
    apply_settled_contract_filter(settled=settled, lottery=lottery, rip_radar=rip)
    assert len(lottery["buy_now"]) == 1
    assert lottery["buy_now"][0]["symbol"] == "NVDA"
    assert lottery["counts"]["buy_now"] == 1
    assert rip["buy_rip"] == []
    assert rip["counts"]["buy_rip"] == 0


def test_journal_dedupe_closed_wins_over_open_same_id(tmp_path):
    tid = "TSM-20261002164321"
    book = TradeJournal(
        starting_cash=5000,
        cash=4800,
        trades=[
            JournalTrade(
                id=tid,
                symbol="TSM",
                contract="TSM261002C00472500",
                expiry="2026-10-02",
                strike=472.5,
                dte_bucket="0dte",
                status="open",
                entry_ask=1.08,
                entry_score=70.0,
                entry_reason="test",
                entered_at="2026-10-02T16:43:21+00:00",
            ),
            JournalTrade(
                id=tid,
                symbol="TSM",
                contract="TSM261002C00472500",
                expiry="2026-10-02",
                strike=472.5,
                dte_bucket="0dte",
                status="closed",
                entry_ask=1.08,
                entry_score=70.0,
                entry_reason="test",
                entered_at="2026-10-02T16:43:21+00:00",
                exited_at="2026-10-02T16:43:21+00:00",
                exit_bid=1.2,
                pnl_usd=12.0,
                profit_pct=11.11,
            ),
        ],
    )
    j = SignalJournal.__new__(SignalJournal)
    j.book = book
    j.starting_cash = 5000
    perf = j.performance()
    assert len(perf["open"]) == 0
    assert len(perf["closed"]) == 1
    assert perf["closed"][0]["id"] == tid
