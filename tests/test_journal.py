from odte_scanner.trading.insights import build_insights
from odte_scanner.trading.journal import SignalJournal


def test_journal_enter_exit_profit_pct(tmp_path):
    path = tmp_path / "journal.json"
    j = SignalJournal(path, starting_cash=5000)
    entered = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "QQQ",
            "contract": "QQQ260805C00722000",
            "expiry": "2026-08-05",
            "strike": 722,
            "ask": 2.0,
            "bid": 1.9,
            "score": 75,
            "dte_bucket": "0dte",
            "detail": "test buy",
            "live_last": 723,
        }
    )
    assert entered is not None
    assert entered.entry_ask == 2.0
    assert j.book.cash == 5000 - 200
    assert entered.cash_before == 5000
    assert entered.cash_after == 4800
    assert entered.equity_after == 5000  # cash + open mark
    assert "Balance after ENTRY" in entered.balance_note
    assert len(j.book.balance_log) == 1
    assert j.book.balance_log[0]["action"] == "ENTRY"
    assert j.book.balance_log[0]["cash_after"] == 4800

    closed = j.exit_from_signal(
        {
            "action": "SELL_NOW",
            "symbol": "QQQ",
            "bid": 2.6,
            "ask": 2.7,
            "detail": "take profit",
            "live_last": 725,
        }
    )
    assert len(closed) == 1
    assert closed[0].profit_pct == 30.0  # (2.6-2.0)/2.0
    assert closed[0].pnl_usd == 60.0
    assert closed[0].status == "closed"
    assert closed[0].cash_before == 4800
    assert closed[0].cash_after == 5060  # 4800 + 260 proceeds
    assert closed[0].equity_after == 5060
    assert "P&L" in closed[0].balance_note
    assert len(j.book.balance_log) == 2
    assert j.book.balance_log[-1]["action"] == "EXIT"
    assert j.book.balance_log[-1]["pnl_usd"] == 60.0
    assert j.book.balance_log[-1]["profit_pct"] == 30.0

    perf = j.performance()
    assert perf["closed_trades"] == 1
    assert perf["win_rate_pct"] == 100.0
    assert perf["avg_profit_pct"] == 30.0
    assert perf["realized_pnl_usd"] == 60.0
    assert perf["cash"] == 5060
    assert len(perf["balance_log"]) == 2


def test_sync_from_actions(tmp_path):
    j = SignalJournal(tmp_path / "j2.json", starting_cash=5000)
    actions = {
        "buy_now_0dte": [
            {
                "action": "BUY_NOW",
                "symbol": "SPY",
                "contract": "SPY260805C00770000",
                "expiry": "2026-08-05",
                "strike": 770,
                "ask": 1.5,
                "score": 80,
                "dte_bucket": "0dte",
                "detail": "buy spy",
            }
        ],
        "buy_now_weekly": [],
        "buy_now": [],
        "sell_now": [],
    }
    sync = j.sync_from_actions(actions)
    assert len(sync["entered"]) == 1
    assert sync["entered"][0]["cash_before"] == 5000
    assert sync["entered"][0]["cash_after"] == 4850
    # Exit
    actions2 = {
        "buy_now_0dte": [],
        "buy_now_weekly": [],
        "buy_now": [],
        "sell_now": [
            {
                "action": "SELL_NOW",
                "symbol": "SPY",
                "bid": 1.2,
                "detail": "stop",
            }
        ],
    }
    sync2 = j.sync_from_actions(actions2)
    assert len(sync2["exited"]) == 1
    assert sync2["exited"][0]["profit_pct"] == -20.0
    assert sync2["exited"][0]["pnl_usd"] == -30.0
    assert sync2["exited"][0]["cash_after"] == 4970
    ins = build_insights(journal=j, actions=actions2)
    assert ins["performance"]["win_rate_pct"] == 0.0
    assert ins["performance"]["avg_profit_pct"] == -20.0
    assert ins["performance"]["realized_pnl_usd"] == -30.0
    assert ins["performance"]["cash"] == 4970
    assert len(ins["balance_log"]) == 2
    assert ins["closed_trades"][0]["balance_note"]


def test_journal_reload_preserves_balance_fields(tmp_path):
    path = tmp_path / "j3.json"
    j = SignalJournal(path, starting_cash=5000)
    j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "IWM",
            "contract": "IWM260805C00250000",
            "expiry": "2026-08-05",
            "strike": 250,
            "ask": 1.0,
            "score": 70,
            "dte_bucket": "0dte",
            "detail": "buy",
        }
    )
    j2 = SignalJournal(path, starting_cash=5000)
    assert len(j2.book.trades) == 1
    assert j2.book.trades[0].cash_before == 5000
    assert j2.book.trades[0].cash_after == 4900
    assert len(j2.book.balance_log) == 1
