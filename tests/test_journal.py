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

    perf = j.performance()
    assert perf["closed_trades"] == 1
    assert perf["win_rate_pct"] == 100.0
    assert perf["avg_profit_pct"] == 30.0


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
    ins = build_insights(journal=j, actions=actions2)
    assert ins["performance"]["win_rate_pct"] == 0.0
    assert ins["performance"]["avg_profit_pct"] == -20.0
