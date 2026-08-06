from odte_scanner.backtest.win_rates import _stats_from_returns, lookup_win_stats
from odte_scanner.signals.actions import build_action_board


def test_stats_from_returns():
    s = _stats_from_returns([1.0, -0.5, 2.0, 0.1])
    assert s["trades"] == 4
    assert s["wins"] == 3
    assert s["win_pct"] == 75.0
    assert s["hit_1pct"] == 50.0


def test_lookup_win_stats_buckets():
    table = {
        "symbols": {
            "QQQ": {
                "0dte": {"win_pct": 58.0, "trades": 40, "hit_1pct": 22.0},
                "weekly": {"win_pct": 61.0, "trades": 35, "hit_1pct": 40.0},
                "swing": {"win_pct": 68.0, "trades": 22, "hit_1pct": 55.0},
            }
        }
    }
    assert lookup_win_stats(table, "QQQ", "0dte")["win_pct"] == 58.0
    assert lookup_win_stats(table, "QQQ", "weekly")["win_pct"] == 61.0
    assert lookup_win_stats(table, "QQQ", "swing")["win_pct"] == 68.0


def test_board_includes_win_pct():
    table = {
        "note": "test",
        "symbols": {
            "SPY": {
                "0dte": {"win_pct": 66.0, "trades": 50, "hit_1pct": 20.0, "forward_days": 1},
                "weekly": {"win_pct": 70.0, "trades": 40, "hit_1pct": 35.0, "forward_days": 5},
                "swing": {"win_pct": 72.0, "trades": 18, "hit_1pct": 50.0, "forward_days": 42},
            }
        },
    }
    board = build_action_board(
        candidates=[
            {
                "symbol": "SPY",
                "score": 80,
                "strike": 770,
                "expiry": "2026-08-05",
                "ask": 1.5,
                "bid": 1.4,
                "contract": "SPY260805C00770000",
                "dte": 0,
                "dte_bucket": "0dte",
            }
        ],
        scores=[{"symbol": "SPY", "ensemble_score": 80}],
        quotes={
            "SPY": {
                "last": 771.5,
                "session_change_pct": 0.3,
                "mom_5m_pct": 0.12,
                "mom_15m_pct": 0.2,
                "dist_from_day_high_pct": -0.05,
            }
        },
        ledger=None,
        buy_score=70,
        win_rate_table=table,
    )
    assert board["buy_now_0dte"]
    assert board["buy_now_0dte"][0]["win_pct"] == 66.0
    assert board["buy_now_0dte"][0]["win_samples"] == 50
    assert "hist win 66%" in board["buy_now_0dte"][0]["detail"]
