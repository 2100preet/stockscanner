from odte_scanner.options.selector import select_calls
from odte_scanner.signals.actions import build_action_board, decide_entry


def test_pltr_uses_real_weekly_not_synthetic():
    # Live chain: PLTR has Fri weeklies, not same-day 0DTE on most Wednesdays
    cands = select_calls(
        "PLTR",
        160.87,
        78.0,
        ["test"],
        max_dte=7,
        odte_max_dte=1,
        otm_pct_max=3.0,
        itm_pct_max=1.5,
        max_ask=25.0,
        per_bucket=1,
    )
    if not cands:
        import pytest

        pytest.skip("Yahoo rate-limited; no live PLTR chain this run")
    assert all(not c.synthetic for c in cands)
    assert all("_SYN" not in c.contract for c in cands)
    weekly = [c for c in cands if c.dte_bucket == "weekly"]
    assert weekly, "PLTR should resolve a weekly call"
    assert weekly[0].strike in {160.0, 162.5, 165.0}
    assert weekly[0].expiry >= "2026-08-07"


def test_synthetic_never_buy_now():
    sig = decide_entry(
        {
            "symbol": "PLTR",
            "score": 90,
            "strike": 163,
            "expiry": "2026-08-05",
            "ask": 0.98,
            "contract": "PLTR_SYN",
            "synthetic": True,
            "dte_bucket": "0dte",
            "dte": 0,
        },
        quote=None,
        buy_score=70,
    )
    assert sig.action == "WAIT"


def test_board_splits_0dte_and_weekly():
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
            },
            {
                "symbol": "PLTR",
                "score": 78,
                "strike": 165,
                "expiry": "2026-08-07",
                "ask": 2.16,
                "bid": 2.13,
                "contract": "PLTR260807C00165000",
                "dte": 2,
                "dte_bucket": "weekly",
            },
        ],
        scores=[{"symbol": "SPY", "ensemble_score": 80}, {"symbol": "PLTR", "ensemble_score": 78}],
        quotes={
            "SPY": {
                "last": 771.5,
                "session_change_pct": 0.3,
                "mom_5m_pct": 0.12,
                "mom_15m_pct": 0.2,
                "dist_from_day_high_pct": -0.05,
            },
            "PLTR": {"last": 163.0, "session_change_pct": 0.5, "mom_5m_pct": 0.1},
        },
        ledger=None,
        buy_score=70,
    )
    assert board["counts"]["buy_now_0dte"] == 1
    assert board["counts"]["buy_now_weekly"] == 1
    assert board["buy_now_weekly"][0]["strike"] == 165
    assert board["counts"]["all"] >= 2
    assert {row["action"] for row in board["all"]} <= {"BUY_NOW", "SELL_NOW", "WAIT", "HOLD"}
    assert all("action" in row for row in board["all"])
