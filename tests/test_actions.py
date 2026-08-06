from odte_scanner.signals.actions import build_action_board, decide_entry, decide_exit


def test_buy_now_requires_short_term_bounce():
    sig = decide_entry(
        {
            "symbol": "SPY",
            "score": 76,
            "strike": 770,
            "expiry": "2026-08-05",
            "ask": 1.5,
            "bid": 1.4,
            "contract": "SPY260805C00770000",
            "dte": 0,
            "dte_bucket": "0dte",
        },
        quote={
            "last": 772,
            "session_change_pct": 0.4,
            "change_pct": 0.4,
            "mom_5m_pct": 0.12,
            "mom_15m_pct": 0.18,
            "dist_from_day_high_pct": -0.1,
        },
        buy_score=70,
    )
    assert sig.action == "BUY_NOW"


def test_qqq_falling_is_wait_not_buy():
    """Regression: stale BUY NOW while QQQ/call are dumping."""
    sig = decide_entry(
        {
            "symbol": "QQQ",
            "score": 77,
            "strike": 724,
            "expiry": "2026-08-05",
            "ask": 1.80,
            "bid": 1.79,
            "contract": "QQQ260805C00724000",
            "dte": 0,
            "dte_bucket": "0dte",
            "option_percent_change": -60.0,
            "moneyness_pct": 0.24,
        },
        quote={
            "last": 722.27,
            "session_change_pct": -0.07,
            "change_pct": -0.07,
            "mom_5m_pct": -0.05,
            "mom_15m_pct": -0.07,
            "dist_from_day_high_pct": -0.86,
        },
        buy_score=70,
    )
    assert sig.action == "WAIT"
    assert "724" in sig.detail or "weak" in sig.detail.lower() or "below" in sig.detail.lower() or "melting" in sig.detail.lower() or "bounce" in sig.detail.lower() or "high" in sig.detail.lower()


def test_wait_on_soft_tape():
    sig = decide_entry(
        {"symbol": "MU", "score": 75, "strike": 880, "expiry": "2026-08-05", "ask": 4.0, "contract": "X", "dte_bucket": "0dte"},
        quote={"last": 876, "session_change_pct": -1.8, "change_pct": 5.0, "mom_5m_pct": -0.2},
        buy_score=70,
    )
    assert sig.action == "WAIT"


def test_sell_now_on_score_collapse():
    sig = decide_exit(
        {"symbol": "SPY", "status": "open", "entry": 1.2, "contract": "C", "id": "1", "score": 70},
        quote={"last": 770, "session_change_pct": 0.1},
        score_by_symbol={"SPY": 40},
        sell_score=48,
    )
    assert sig is not None
    assert sig.action == "SELL_NOW"


def test_action_board_primary_prefers_sell():
    board = build_action_board(
        candidates=[
            {
                "symbol": "QQQ",
                "score": 80,
                "strike": 720,
                "expiry": "2026-08-05",
                "ask": 2.0,
                "bid": 1.9,
                "contract": "QQQ260805C00720000",
                "dte": 0,
                "dte_bucket": "0dte",
            }
        ],
        scores=[{"symbol": "QQQ", "ensemble_score": 80}, {"symbol": "SPY", "ensemble_score": 40}],
        quotes={
            "QQQ": {
                "last": 721,
                "session_change_pct": 0.2,
                "mom_5m_pct": 0.15,
                "mom_15m_pct": 0.2,
                "dist_from_day_high_pct": -0.05,
            },
            "SPY": {"last": 770, "session_change_pct": -1.5, "mom_5m_pct": -0.4},
        },
        ledger={
            "trades": [
                {"symbol": "SPY", "status": "open", "entry": 1.1, "contract": "SPYC", "id": "t1", "score": 72}
            ]
        },
        buy_score=70,
        sell_score=48,
    )
    assert board["primary"]["action"] == "SELL_NOW"
    assert board["counts"]["sell_now"] >= 1
