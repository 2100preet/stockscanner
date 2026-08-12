"""Regression: offline Pages must still enter/exit paper trades with EXIT plans."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import build_action_board
from odte_scanner.trading.journal import SignalJournal

ET = ZoneInfo("America/New_York")
_MORNING = datetime(2026, 8, 12, 11, 0, tzinfo=ET)
_LATE = datetime(2026, 8, 12, 15, 50, tzinfo=ET)


def test_offline_board_can_buy_without_live_tape():
    board = build_action_board(
        candidates=[
            {
                "symbol": "MSFT",
                "score": 82,
                "strike": 420,
                "expiry": "2026-08-14",
                "ask": 2.0,
                "bid": 1.9,
                "contract": "MSFT260814C00420000",
                "dte": 2,
                "dte_bucket": "weekly",
                "right": "C",
            }
        ],
        scores=[{"symbol": "MSFT", "ensemble_score": 82}],
        quotes={},
        ledger=None,
        require_hist_win=False,
        require_live_confirm=False,
        now=_MORNING,
    )
    assert board["counts"]["buy_now"] >= 1
    buy = board["buy_now"][0]
    assert buy["exit_plan"]
    assert "TP" in buy["exit_plan"]
    assert board["hold_rules"]["exit_criteria"]


def test_journal_enter_then_time_stop_exit(tmp_path):
    j = SignalJournal(tmp_path / "j.json", starting_cash=5000)
    board = build_action_board(
        candidates=[
            {
                "symbol": "SPY",
                "score": 80,
                "strike": 560,
                "expiry": "2026-08-12",
                "ask": 1.5,
                "bid": 1.4,
                "contract": "SPY260812C00560000",
                "dte": 0,
                "dte_bucket": "0dte",
                "right": "C",
                "headline": "BUY NOW SPY",
                "detail": "test",
            }
        ],
        scores=[{"symbol": "SPY", "ensemble_score": 80}],
        quotes={
            "SPY": {
                "last": 561,
                "session_change_pct": 0.3,
                "mom_5m_pct": 0.12,
                "mom_15m_pct": 0.2,
                "dist_from_day_high_pct": -0.05,
            }
        },
        ledger=None,
        require_hist_win=False,
        require_live_confirm=True,
        now=_MORNING,
    )
    # Attach headline so enter_from_signal path via sync keeps desk-quality rows
    for row in board["buy_now"]:
        row.setdefault("headline", row.get("headline") or "BUY NOW")
        row.setdefault("detail", row.get("detail") or "enter")
    sync = j.sync_from_actions(board, auto_enter=True, auto_exit=True)
    assert len(sync["entered"]) >= 1
    assert (tmp_path / "j.json").exists()

    opens = [
        {
            **t.to_dict(),
            "entry": t.entry_ask,
            "bid": t.mark or t.entry_ask,
            "mark": t.mark or t.entry_ask,
            "status": "open",
        }
        for t in j.book.trades
        if t.status == "open"
    ]
    late = build_action_board(
        candidates=[],
        scores=[{"symbol": "SPY", "ensemble_score": 70}],
        quotes={"SPY": {"last": 561, "session_change_pct": 0.1, "mom_5m_pct": 0.0}},
        ledger=None,
        journal_opens=opens,
        require_hist_win=False,
        now=_LATE,
    )
    assert late["counts"]["sell_now"] >= 1
    assert "time-stop" in late["sell_now"][0]["detail"].lower() or "15:45" in late["sell_now"][0]["detail"]
    sync2 = j.sync_from_actions(late, auto_enter=False, auto_exit=True)
    assert len(sync2["exited"]) >= 1
    closed = sync2["exited"][0]
    assert closed["exited_at"]
    assert closed["profit_pct"] is not None
    perf = j.performance()
    assert perf["closed_trades"] >= 1


def test_sync_skips_raw_candidates_without_headline(tmp_path):
    j = SignalJournal(tmp_path / "j2.json", starting_cash=5000)
    sync = j.sync_from_actions(
        {
            "buy_now": [
                {
                    "action": "BUY_NOW",
                    "symbol": "NVDA",
                    "ask": 2.0,
                    "contract": "NVDA260812C00100000",
                    "strike": 100,
                    "expiry": "2026-08-12",
                    # no headline/detail/exit_plan — polluted scan row
                }
            ],
            "buy_now_0dte": [],
            "buy_now_weekly": [],
            "sell_now": [],
        },
        auto_enter=True,
        auto_exit=False,
    )
    assert sync["entered"] == []
    assert (tmp_path / "j2.json").exists()
