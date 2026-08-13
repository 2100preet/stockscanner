from datetime import datetime, timedelta, timezone

from odte_scanner.challenge.million import (
    build_challenge_board,
    compound_path,
    path_table,
    time_boxed_path,
    _side_from_tape,
)
from odte_scanner.challenge.tracker import ChallengeTracker, hold_period_for


def test_compound_path_12_flips():
    p = compound_path(start_usd=1000, target_usd=1_000_000, flips=12)
    assert p["pct_per_flip"] > 70  # ~78%
    assert p["schedule"][-1]["equity"] >= 999_000


def test_time_boxed_path_4mo_500k_weekly():
    pace = time_boxed_path(
        start_usd=1000,
        milestone_usd=500_000,
        target_usd=1_000_000,
        months=4,
        ideal_hold_days=8,
    )
    assert pace["flips_in_window"] >= 12
    assert pace["milestone"]["pct_per_flip"] > 40
    assert pace["schedule"][-1]["hit_milestone"] is True
    assert pace["feasible"] is True


def test_challenge_enter_exit_records_balance(tmp_path):
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(ledger, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "JPM",
        "right": "C",
        "ask": 2.0,
        "contract": "JPM260918C00200000",
        "expiry": "2026-09-18",
        "strike": 200,
        "horizon": "weekly",
        "dte": 30,
        "target_premium_mult": 1.6,
        "spot": 200,
    }
    entered = tr.enter(ticket)
    assert entered is not None
    assert entered.cash_before == 1000
    assert entered.cash_after == 800  # 2*100*1
    assert entered.equity_after == 1000
    assert tr.book.balance_log[-1]["action"] == "ENTRY"
    out = tr.exit_trade(entered.id, exit_bid=3.2, reason="target")
    assert out is not None
    assert out.cash_after == 800 + 320
    assert out.pnl_usd == 120
    assert tr.book.balance_log[-1]["action"] == "EXIT"
    assert tr.book.cash == out.cash_after


def test_path_table_10_to_15():
    rows = path_table()
    assert [r["flips"] for r in rows] == list(range(10, 16))
    assert rows[0]["pct_per_flip"] > rows[-1]["pct_per_flip"]


def test_hold_periods_weekly_swing_leap():
    w = hold_period_for("weekly")
    s = hold_period_for("swing")
    leap = hold_period_for("swing", 200)
    assert w["style"] == "weekly" and w["min_days"] == 5 and w["max_days"] == 14
    assert s["style"] == "swing" and s["min_days"] == 20 and s["max_days"] == 60
    assert leap["style"] == "leap" and leap["min_days"] == 30 and leap["max_days"] == 90
    assert "d" in w["label"]


def test_side_from_tape_calls_and_puts():
    assert _side_from_tape(score={"ensemble_score": 78, "bullish": True}, quote={"mom_5m_pct": 0.2}) == "C"
    assert (
        _side_from_tape(
            score={"ensemble_score": 40, "bullish": False},
            quote={"mom_5m_pct": -0.3, "session_change_pct": -1.5},
        )
        == "P"
    )


def test_challenge_board_picks_perfect_hist():
    win_table = {
        "symbols": {
            "JPM": {
                "weekly": {
                    "win_pct": 100.0,
                    "trades": 4,
                    "wins": 4,
                    "hit_1pct": 75.0,
                    "hit_2pct": 50.0,
                },
                "swing": {"win_pct": 50.0, "trades": 8, "wins": 4},
            },
            "SLV": {
                "swing": {
                    "win_pct": 90.9,
                    "trades": 11,
                    "wins": 10,
                    "hit_1pct": 90.9,
                    "hit_2pct": 90.9,
                }
            },
            "BAD": {"swing": {"win_pct": 40.0, "trades": 20, "wins": 8}},
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {"symbol": "JPM", "horizon": "weekly", "ensemble_score": 72, "quality": True, "last_price": 200},
            {"symbol": "SLV", "horizon": "swing", "ensemble_score": 76, "quality": True, "last_price": 28},
        ],
        quotes={"JPM": {"last": 200}, "SLV": {"last": 28}},
        fetch_contracts=False,
        fetch_earnings=False,
        flips=12,
    )
    assert board["path"]["flips"] == 12
    syms = [t["symbol"] for t in board["tickets"]]
    assert "JPM" in syms
    assert "SLV" in syms
    assert "BAD" not in syms
    assert board["primary"]["certainty_tier"] in {"perfect", "elite", "strong"}
    assert board["primary"]["symbol"] == "JPM"
    assert "disclaimer" in board
    # Hold period + ENTRY/HOLD/EXIT fields on every ticket
    for t in board["tickets"]:
        assert t["action"] in {"ENTRY", "HOLD", "EXIT", "WAIT"}
        assert t["right"] in {"C", "P"}
        assert t["hold_period_label"]
        assert t["hold_approx_label"]
        assert t["approx_hold_days"] > 0
        assert t["hold_min_days"] > 0
        assert t["hold_max_days"] >= t["hold_min_days"]
        assert t["recommend_reason"]
        assert isinstance(t["reasons"], list) and len(t["reasons"]) >= 3
        assert any("Approx hold" in r for r in t["reasons"])
        assert t["market_cap_tier"]
        assert t["spot_source"] in {"live", "cache", "scan", "none"}
    assert "hold_periods" in board
    # Without live chains, tickets stay WAIT (liquidity gate) — still return plans
    assert board["counts"]["tickets"] >= 1
    assert board["counts"]["calls"] + board["counts"]["puts"] == board["counts"]["tickets"]
    assert board["primary"]["recommend_reason"]
    assert "≈" in board["primary"]["hold_approx_label"]
    assert board["primary"]["enter_plan"]
    assert board["primary"]["exit_plan"]
    assert board["primary"]["target_profit_pct"] > 50
    assert board["primary"]["action"] in {"ENTRY", "HOLD", "EXIT", "WAIT"}


def test_challenge_board_put_side_and_hold_status():
    win_table = {
        "symbols": {
            "XOM": {
                "swing": {
                    "win_pct": 100.0,
                    "trades": 5,
                    "wins": 5,
                    "hit_1pct": 80.0,
                    "hit_2pct": 60.0,
                }
            }
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {
                "symbol": "XOM",
                "horizon": "swing",
                "ensemble_score": 38,
                "bullish": False,
                "quality": True,
                "last_price": 110,
            }
        ],
        quotes={"XOM": {"last": 110, "mom_5m_pct": -0.4, "session_change_pct": -1.8}},
        fetch_contracts=False,
        fetch_earnings=False,
        flips=12,
    )
    assert board["tickets"]
    t0 = board["tickets"][0]
    assert t0["right"] == "P"
    # No live chain in unit test → WAIT on liquidity gate (still PUT-sided)
    assert t0["action"] in {"ENTRY", "WAIT"}
    assert "PUT" in t0["recommend_reason"]
    assert any("PUT" in r or "tape" in r for r in t0["reasons"])
    assert t0["hold_period_label"]
    assert t0["market_cap_tier"] == "mega_large"


def test_challenge_board_midcap_reasons():
    win_table = {
        "symbols": {
            "DKNG": {
                "swing": {
                    "win_pct": 100.0,
                    "trades": 5,
                    "wins": 5,
                    "hit_1pct": 80.0,
                    "hit_2pct": 60.0,
                }
            }
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {"symbol": "DKNG", "horizon": "swing", "ensemble_score": 74, "quality": True, "last_price": 40}
        ],
        quotes={"DKNG": {"last": 40, "mom_5m_pct": 0.2}},
        fetch_contracts=False,
        fetch_earnings=False,
        flips=12,
    )
    t0 = board["tickets"][0]
    assert t0["symbol"] == "DKNG"
    assert t0["market_cap_tier"] == "mid"
    assert any("Mid-cap" in r for r in t0["reasons"])


def test_challenge_board_hold_and_exit_from_open_trade(tmp_path):
    win_table = {
        "symbols": {
            "AAPL": {
                "swing": {"win_pct": 100.0, "trades": 6, "wins": 6, "hit_1pct": 70.0, "hit_2pct": 50.0}
            }
        }
    }
    entered = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    open_trades = [
        {
            "id": "CH-AAPLC-1",
            "symbol": "AAPL",
            "right": "C",
            "contract": "AAPL250117C00200000",
            "expiry": "2025-01-17",
            "strike": 200.0,
            "horizon": "swing",
            "dte_at_entry": 180,
            "entered_at": entered,
            "entry_ask": 5.0,
            "status": "open",
            "hold_min_days": 20,
            "hold_max_days": 60,
            "hold_days": 40.0,
            "mark": 10.0,  # +100% → EXIT target (~78%)
            "last_action": "HOLD",
            "last_action_detail": "holding",
        }
    ]
    board = build_challenge_board(
        win_table=win_table,
        scores=[{"symbol": "AAPL", "horizon": "swing", "ensemble_score": 70, "last_price": 190}],
        quotes={"AAPL": {"last": 190}},
        open_trades=open_trades,
        fetch_contracts=False,
        fetch_earnings=False,
        flips=12,
    )
    t0 = board["primary"]
    assert t0["symbol"] == "AAPL"
    assert t0["right"] == "C"
    assert t0["action"] == "EXIT"
    assert board["counts"]["exit"] >= 1
    assert t0["hold_days"] == 40.0


def test_tracker_enter_hold_exit_call_and_put(tmp_path):
    path = tmp_path / "challenge_ledger.json"
    tr = ChallengeTracker(path, starting_cash=1000)
    call_ticket = {
        "action": "ENTRY",
        "symbol": "MSFT",
        "right": "C",
        "ask": 4.0,
        "contract": "MSFT250117C00400000",
        "expiry": "2025-01-17",
        "strike": 400,
        "horizon": "swing",
        "dte": 200,
        "spot": 390,
        "target_premium_mult": 1.78,
        "contracts_for_bankroll": 1,
        "thesis": "test call",
    }
    entered = tr.enter(call_ticket)
    assert entered is not None
    assert entered.right == "C"
    assert entered.hold_max_days == 90  # LEAP by DTE
    ev = tr.evaluate_open(entered, mark=4.2, quote={"mom_5m_pct": 0.05})
    assert ev["action"] == "HOLD"
    assert entered.last_action == "HOLD"

    # Force EXIT via target
    ev2 = tr.evaluate_open(entered, mark=8.0, quote={})
    assert ev2["action"] == "EXIT"
    out = tr.exit_trade(entered.id, exit_bid=8.0, reason=ev2["detail"])
    assert out is not None and out.status == "closed"
    assert tr.book.wins == 1

    put_ticket = {
        "action": "ENTRY",
        "symbol": "META",
        "right": "P",
        "ask": 3.5,
        "contract": "META250117P00450000",
        "expiry": "2025-01-17",
        "strike": 450,
        "horizon": "weekly",
        "dte": 90,
        "spot": 480,
        "target_premium_mult": 1.78,
        "contracts_for_bankroll": 1,
        "thesis": "test put",
    }
    put = tr.enter(put_ticket)
    assert put is not None
    assert put.right == "P"
    assert put.hold_min_days == 5
    assert put.hold_max_days == 14
