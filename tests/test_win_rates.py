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
                "monthly": {"win_pct": 65.0, "trades": 28, "hit_1pct": 48.0, "hit_2pct": 30.0},
                "swing": {"win_pct": 68.0, "trades": 22, "hit_1pct": 55.0},
            }
        }
    }
    assert lookup_win_stats(table, "QQQ", "0dte")["win_pct"] == 58.0
    assert lookup_win_stats(table, "QQQ", "weekly")["win_pct"] == 61.0
    assert lookup_win_stats(table, "QQQ", "swing")["win_pct"] == 68.0
    # 1-month / leap aliases → monthly strike-rate (not 0dte)
    m = lookup_win_stats(table, "QQQ", "leap")
    assert m["win_pct"] == 65.0
    assert m["hit_1pct"] == 48.0
    assert m["horizon"] == "monthly"
    assert lookup_win_stats(table, "QQQ", "1m")["hit_1pct"] == 48.0
    assert lookup_win_stats(table, "QQQ", "monthly")["hit_2pct"] == 30.0


def test_lookup_monthly_falls_back_to_swing():
    table = {
        "symbols": {
            "DELL": {
                "swing": {"win_pct": 80.0, "trades": 15, "hit_1pct": 80.0, "hit_2pct": 80.0},
            }
        }
    }
    s = lookup_win_stats(table, "DELL", "leap")
    assert s["hit_1pct"] == 80.0
    assert s["win_pct"] == 80.0


def test_board_includes_win_pct():
    table = {
        "note": "test",
        "symbols": {
            "SPY": {
                "0dte": {"win_pct": 82.0, "trades": 50, "wins": 41, "hit_1pct": 20.0, "forward_days": 1},
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
        require_hist_win=True,
        min_hist_win_pct=80,
        min_hist_win_samples=5,
    )
    assert board["buy_now_0dte"]
    assert board["buy_now_0dte"][0]["win_pct"] == 82.0
    assert board["buy_now_0dte"][0]["win_samples"] == 50
    assert board["buy_now_0dte"][0]["hit_1pct"] == 20.0
    detail = board["buy_now_0dte"][0]["detail"]
    assert "hist win 82%" in detail
    assert "n=50 samples" in detail
    assert "strike rate ≥1% 20%" in detail
    assert board["hist_win_gate"]["pooled_win_pct"] >= 80
