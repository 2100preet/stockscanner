from odte_scanner.backtest.win_rates import summarize_hist_win_gate


def test_summarize_hist_win_gate_pools_eligible_only():
    table = {
        "symbols": {
            "AAA": {
                "0dte": {"win_pct": 90.0, "trades": 10, "wins": 9, "hit_1pct": 50.0},
                "weekly": {"win_pct": 40.0, "trades": 10, "wins": 4, "hit_1pct": 20.0},
            },
            "BBB": {
                "0dte": {"win_pct": 100.0, "trades": 2, "wins": 2, "hit_1pct": 50.0},  # n too small
            },
            "CCC": {
                "swing": {"win_pct": 80.0, "trades": 5, "wins": 4, "hit_1pct": 60.0},
            },
        }
    }
    g = summarize_hist_win_gate(table, min_hist_win_pct=80, min_hist_win_samples=5)
    assert g["eligible_count"] == 2  # AAA 0dte + CCC swing
    assert g["pooled_trades"] == 15
    assert g["pooled_win_pct"] == round(100.0 * (9 + 4) / 15, 1)
    assert g["target_met"] is True
    assert g["ungated_pooled_trades"] == 27
