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


def test_hold_periods_sprint_and_short_dte():
    sp = hold_period_for("sprint")
    assert sp["style"] == "sprint" and sp["min_days"] == 0 and sp["max_days"] == 1
    assert sp["ideal_days"] == 1
    # Short-dated contracts map to sprint even if hist horizon was swing
    short = hold_period_for("swing", 5)
    assert short["style"] == "sprint" and short["max_days"] == 1


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
        assert t["hold_min_days"] >= 0
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
    assert board["primary"]["target_profit_pct"] >= 50
    assert board["primary"]["target_profit_pct"] <= 100
    assert board["primary"]["action"] in {"ENTRY", "HOLD", "EXIT", "WAIT"}
    assert board["primary"]["hold_max_days"] <= 3
    assert board["primary"]["pace_style"] == "sprint"
    assert "sprint" in board["hold_periods"]


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


def test_eligible_rows_widens_beyond_single_perfect():
    from odte_scanner.challenge.million import _eligible_rows

    win_table = {
        "symbols": {
            "TOST": {"swing": {"win_pct": 100.0, "trades": 4, "wins": 4, "hit_1pct": 75.0}},
            "DKNG": {"swing": {"win_pct": 78.0, "trades": 4, "wins": 3, "hit_1pct": 70.0}},
            "JPM": {"weekly": {"win_pct": 85.0, "trades": 6, "wins": 5, "hit_1pct": 80.0}},
        }
    }
    rows = _eligible_rows(win_table)
    syms = {r["symbol"] for r in rows}
    assert "TOST" in syms
    assert "JPM" in syms
    assert len(syms) >= 2


def test_challenge_hist_universe_covers_mid_small_breadth():
    from odte_scanner.data.universe import challenge_hist_universe

    syms = challenge_hist_universe()
    assert len(syms) >= 30
    assert "TOST" in syms
    assert "DKNG" in syms


def test_challenge_board_multiple_tickets_not_single_name():
    win_table = {
        "symbols": {
            "TOST": {"swing": {"win_pct": 100.0, "trades": 4, "wins": 4, "hit_1pct": 75.0, "hit_2pct": 50.0}},
            "JPM": {"weekly": {"win_pct": 100.0, "trades": 5, "wins": 5, "hit_1pct": 80.0, "hit_2pct": 60.0}},
            "SLV": {"swing": {"win_pct": 90.9, "trades": 11, "wins": 10, "hit_1pct": 90.9, "hit_2pct": 90.9}},
            "DKNG": {"swing": {"win_pct": 100.0, "trades": 5, "wins": 5, "hit_1pct": 80.0, "hit_2pct": 60.0}},
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[],
        quotes={},
        fetch_contracts=False,
        fetch_earnings=False,
        max_tickets=8,
    )
    syms = [t["symbol"] for t in board["tickets"]]
    assert len(syms) >= 3
    assert syms.count("TOST") == 1


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
    # Explicit frac: $400 ticket needs room; sprint desk hard-skips over-frac by default
    entered = tr.enter(call_ticket, max_cash_frac=0.50)
    assert entered is not None
    assert entered.right == "C"
    assert entered.hold_max_days == 90  # LEAP by DTE
    # Legacy long-dated evaluate (sprint_desk off)
    ev = tr.evaluate_open(
        entered, mark=4.2, quote={"mom_5m_pct": 0.05}, sprint_desk=False
    )
    assert ev["action"] == "HOLD"
    assert entered.last_action == "HOLD"

    # Sprint desk retires long-dated opens so cash can flip 1–3d tickets
    retired = tr.evaluate_open(entered, mark=4.2, quote={}, sprint_desk=True)
    assert retired["action"] == "EXIT"
    assert "sprint" in retired["detail"].lower()

    # Force EXIT via target (sprint off so target path is tested cleanly)
    ev2 = tr.evaluate_open(entered, mark=8.0, quote={}, sprint_desk=False)
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
        "dump_confirm": True,
        "ensemble_score": 70,
    }
    put = tr.enter(put_ticket)
    assert put is not None
    assert put.right == "P"
    assert put.hold_min_days == 5
    assert put.hold_max_days == 14

    sprint_ticket = {
        "action": "ENTRY",
        "symbol": "NVDA",
        "right": "C",
        "ask": 2.5,
        "contract": "NVDA250912C00180000",
        "expiry": "2025-09-12",
        "strike": 180,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 3,
        "spot": 175,
        "target_premium_mult": 1.9,
        "contracts_for_bankroll": 1,
        "thesis": "sprint flip",
    }
    # Flat book after prior exits
    if tr.open_trades():
        for ot in list(tr.open_trades()):
            tr.exit_trade(ot.id, exit_bid=float(ot.entry_ask), reason="clear")
    sprint = tr.enter(sprint_ticket)
    assert sprint is not None
    assert sprint.hold_min_days == 0
    assert sprint.hold_max_days == 1
    assert 1.5 <= sprint.target_premium_mult <= 2.0


def test_sync_exits_with_live_mark_not_flat_entry(tmp_path):
    """Max-hold EXIT must book live bid P&L — not silently $0 at entry ask."""
    path = tmp_path / "challenge_ledger.json"
    tr = ChallengeTracker(path, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "TSLA",
        "right": "C",
        "ask": 2.0,
        "contract": "TSLA260918C00250000",
        "expiry": "2026-09-18",
        "strike": 250,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 3,
        "spot": 245,
        "target_premium_mult": 1.75,
        "contracts_for_bankroll": 1,
        "thesis": "sprint",
        "hold_min_days": 1,
        "hold_max_days": 3,
        "hold_ideal_days": 2,
    }
    entered = tr.enter(ticket)
    assert entered is not None
    # Force past max hold
    from datetime import datetime, timedelta, timezone
    entered.entered_at = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    tr.save()

    # Ticket still carries stale bid=entry (the old bug path)
    stale = {
        "symbol": "TSLA",
        "right": "C",
        "action": "HOLD",
        "bid": 2.0,
        "ask": 2.0,
        "contract": entered.contract,
    }
    sync = tr.sync_from_tickets(
        [stale],
        auto_enter=False,
        auto_exit=True,
        sprint_desk=True,
        live_marks={entered.id: 3.4},  # live bid up +70%
    )
    assert sync["exited"] == [entered.id]
    closed = next(x for x in tr.book.trades if x.id == entered.id)
    assert closed.status == "closed"
    assert closed.exit_bid == 3.4
    assert closed.pnl_usd == 140.0  # (3.4-2)*100
    assert closed.profit_pct == 70.0


def test_sync_annotates_flat_exit_without_live_mark(tmp_path):
    path = tmp_path / "challenge_ledger.json"
    tr = ChallengeTracker(path, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "AMD",
        "right": "P",
        "ask": 1.5,
        "contract": "AMD260918P00150000",
        "expiry": "2026-09-18",
        "strike": 150,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 2,
        "spot": 155,
        "target_premium_mult": 1.75,
        "hold_min_days": 1,
        "hold_max_days": 3,
        "hold_ideal_days": 2,
        "dump_confirm": True,
        "ensemble_score": 70,
    }
    entered = tr.enter(ticket)
    from datetime import datetime, timedelta, timezone
    entered.entered_at = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    tr.save()
    sync = tr.sync_from_tickets(
        [{"symbol": "AMD", "right": "P", "bid": 1.5, "ask": 1.5}],
        auto_enter=False,
        auto_exit=True,
        sprint_desk=True,
        live_marks={},
    )
    assert sync["exited"] == [entered.id]
    closed = next(x for x in tr.book.trades if x.id == entered.id)
    # Stale entry mark past max hold → salvage residual (not fake $0 flat)
    assert closed.pnl_usd is not None and closed.pnl_usd < 0
    assert "salvage" in (closed.exit_reason or "").lower() or "max hold" in (closed.exit_reason or "").lower()


def test_compound_path_15_flips_1mo_pace():
    p = compound_path(start_usd=1000, target_usd=1_000_000, flips=15)
    assert 55 <= p["pct_per_flip"] <= 65  # ~58%
    assert p["schedule"][-1]["equity"] >= 999_000


def test_time_boxed_path_1mo_1m_sprint():
    pace = time_boxed_path(
        start_usd=1000,
        milestone_usd=1_000_000,
        target_usd=1_000_000,
        months=1,
        ideal_hold_days=2,
    )
    assert pace["style"] == "sprint"
    assert pace["flips_in_window"] >= 14
    assert pace["milestone"]["pct_per_flip"] > 50
    assert pace["schedule"][-1]["hit_milestone"] is True
    assert "1-month" in pace["note"] or "1-month" in pace["note"].replace(" ", "") or "1" in pace["note"]


def test_side_from_tape_sprint_needs_dump_for_puts():
    # Weak bearish without dump → call on sprint desk (stops SLV-put grind-up loop)
    assert (
        _side_from_tape(
            score={"ensemble_score": 44, "bullish": False},
            quote={"mom_5m_pct": -0.05, "session_change_pct": -0.2},
            sprint=True,
        )
        == "C"
    )
    # Clear dump → put
    assert (
        _side_from_tape(
            score={"ensemble_score": 40, "bullish": False},
            quote={"mom_5m_pct": -0.4, "session_change_pct": -1.5},
            sprint=True,
        )
        == "P"
    )


def test_loss_cooldown_blocks_reentry(tmp_path):
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(ledger, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "SLV",
        "right": "P",
        "ask": 2.0,
        "contract": "SLV260918P00028000",
        "expiry": "2026-09-18",
        "strike": 28,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 3,
        "spot": 30,
        "target_premium_mult": 1.75,
        "contracts_for_bankroll": 1,
        "dump_confirm": True,
        "ensemble_score": 70,
    }
    entered = tr.enter(ticket)
    assert entered is not None
    out = tr.exit_trade(entered.id, exit_bid=0.8, reason="stop")
    assert out is not None and out.pnl_usd < 0
    blocked = tr.recent_loss_symbols(cooldown_days=5)
    assert "SLV" in blocked
    again = tr.enter(ticket, loss_cooldown_days=5)
    assert again is None


def test_loss_cooldown_survives_epoch_archive(tmp_path):
    """Archived Monday losers must still block Challenge ENTRY after rebuild."""
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="sprint-v1",
        rebuild_seed_usd=1000,
    )
    ticket = {
        "action": "ENTRY",
        "symbol": "JPM",
        "right": "C",
        "ask": 1.0,
        "contract": "JPM260925C00345000",
        "expiry": "2026-09-25",
        "strike": 345,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 2,
        "spot": 340,
        "target_premium_mult": 1.5,
        "ensemble_score": 70,
    }
    entered = tr.enter(ticket)
    assert entered is not None
    out = tr.exit_trade(entered.id, exit_bid=0.2, reason="stop")
    assert out is not None and out.pnl_usd < 0
    # Bump epoch → archives the loss off the live book
    tr2 = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="sprint-v2",
        rebuild_seed_usd=1000,
        rebuild_reason="test rebuild",
    )
    assert tr2.book.trades == []
    assert tr2.book.archive
    assert "JPM" in tr2.recent_loss_symbols(cooldown_days=7)
    again = tr2.enter(
        {**ticket, "contract": "JPM261002C00345000", "expiry": "2026-10-02"},
        loss_cooldown_days=7,
    )
    assert again is None


def test_max_cash_frac_caps_contracts(tmp_path):
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(ledger, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "NVDA",
        "right": "C",
        "ask": 2.0,
        "contract": "NVDA260918C00180000",
        "expiry": "2026-09-18",
        "strike": 180,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 2,
        "spot": 175,
        "target_premium_mult": 1.75,
        "contracts_for_bankroll": 10,  # would be $2000 without cap
    }
    entered = tr.enter(ticket, max_cash_frac=0.35)
    assert entered is not None
    assert entered.contracts == 1  # 35% of 1000 = $350 → 1 contract @ $200
    assert entered.cost == 200.0


def test_bank_sprint_40pct_early(tmp_path):
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(ledger, starting_cash=1000)
    ticket = {
        "action": "ENTRY",
        "symbol": "AAPL",
        "right": "C",
        "ask": 2.0,
        "contract": "AAPL260918C00200000",
        "expiry": "2026-09-18",
        "strike": 200,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 1,
        "spot": 198,
        "target_premium_mult": 1.9,  # +90% full target
        "hold_min_days": 0,
        "hold_max_days": 1,
        "hold_ideal_days": 1,
        "ensemble_score": 70,
    }
    entered = tr.enter(ticket)
    assert entered is not None
    entered.entered_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    tr.save()
    ev = tr.evaluate_open(entered, mark=2.85, quote={}, sprint_desk=True)  # +42.5%
    assert ev["action"] == "EXIT"
    assert "bank sprint" in ev["detail"].lower() or "40" in ev["detail"]


def test_board_waits_loss_cooldown_symbols():
    win_table = {
        "symbols": {
            "SLV": {
                "swing": {
                    "win_pct": 100.0,
                    "trades": 6,
                    "wins": 6,
                    "hit_1pct": 80.0,
                    "hit_2pct": 60.0,
                }
            }
        }
    }
    board = build_challenge_board(
        win_table=win_table,
        scores=[
            {"symbol": "SLV", "horizon": "swing", "ensemble_score": 70, "quality": True, "last_price": 28}
        ],
        quotes={"SLV": {"last": 28, "mom_5m_pct": 0.2}},
        fetch_contracts=False,
        fetch_earnings=False,
        flips=15,
        pace_months=1.0,
        pace_milestone_usd=1_000_000,
        loss_cooldown_symbols={"SLV"},
    )
    assert board["path"]["flips"] == 15
    assert board["pace"]["months"] == 1
    t0 = board["tickets"][0]
    assert t0["symbol"] == "SLV"
    assert t0["action"] == "WAIT"
    assert "cooldown" in (t0.get("status_detail") or t0.get("detail") or "").lower() or any(
        "cooldown" in r.lower() for r in (t0.get("reasons") or [])
    )


def test_epoch_rebuild_reseeds_honest_1k(tmp_path):
    """Fake pace seeds are banned — rebuild must restart at real $1k."""
    ledger = tmp_path / "ch.json"
    ledger.write_text(
        '{"starting_cash":30000,"cash":30000,"wins":0,"losses":0,"flips_closed":0,'
        '"trades":[],"balance_log":[],"epoch":"2026-09-28-pace30k"}'
    )
    tr = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="2026-09-29-1k-fastflips",
        rebuild_seed_usd=1000,
        rebuild_reason="honest restart",
    )
    assert tr.book.cash == 1000
    assert tr.book.equity == 1000
    assert tr.book.starting_cash == 1000
    assert tr.book.wins == 0 and tr.book.losses == 0
    assert tr.book.archive and tr.book.archive[-1]["prior_cash"] == 30000
    # second load is idempotent
    tr2 = ChallengeTracker(
        ledger, starting_cash=1000, epoch="2026-09-29-1k-fastflips", rebuild_seed_usd=1000
    )
    assert tr2.book.cash == 1000


def test_skip_over_cash_frac_and_ask_band(tmp_path):
    """$500k sprint: never force a 1ct that blows the cash-frac / ask band."""
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(ledger, starting_cash=1000)
    rich = {
        "action": "ENTRY",
        "symbol": "MDB",
        "right": "C",
        "ask": 4.0,  # $400 > 30% of $1k
        "contract": "MDB261002C00360000",
        "expiry": "2026-10-02",
        "strike": 360,
        "horizon": "sprint",
        "hold_style": "sprint",
        "dte": 1,
        "spot": 355,
        "target_premium_mult": 1.5,
        "ensemble_score": 70,
        "volume": 500,
    }
    assert tr.enter(rich, max_cash_frac=0.30, max_ask=2.50) is None
    assert tr.enter(rich, max_cash_frac=0.50, max_ask=2.50) is None  # ask band
    penny = {**rich, "symbol": "LUNR", "ask": 0.05, "contract": "LUNR261002C00015000", "strike": 15}
    assert tr.enter(penny, min_ask=0.20, max_ask=2.50, max_cash_frac=0.30) is None
    ok = {
        **rich,
        "symbol": "NVDA",
        "ask": 1.50,
        "contract": "NVDA261002C00190000",
        "strike": 190,
        "spot": 188,
    }
    entered = tr.enter(ok, max_cash_frac=0.30, min_ask=0.20, max_ask=2.50, min_ensemble=60)
    assert entered is not None
    assert entered.cost == 150.0
    assert entered.contracts == 1


def test_500k_epoch_unpauses_after_three_losses(tmp_path):
    """Death-spiral pause clears on epoch bump; archived losers stay on cooldown."""
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="2026-09-29-1k-fastflips",
        rebuild_seed_usd=1000,
    )
    for i, sym in enumerate(["LUNR", "QUBT", "GOOGL"]):
        t = tr.enter(
            {
                "action": "ENTRY",
                "symbol": sym,
                "right": "C",
                "ask": 0.50,
                "contract": f"{sym}261002C00100000",
                "expiry": "2026-10-02",
                "strike": 100,
                "horizon": "sprint",
                "hold_style": "sprint",
                "dte": 1,
                "spot": 100,
                "target_premium_mult": 1.5,
                "ensemble_score": 70,
                "volume": 200,
            },
            max_consecutive_losses=3,
            max_cash_frac=0.30,
            min_ask=0.20,
            max_ask=2.50,
        )
        assert t is not None, sym
        out = tr.exit_trade(t.id, exit_bid=0.10, reason="stop")
        assert out is not None and out.pnl_usd < 0
    assert tr.consecutive_losses() == 3
    sync = tr.sync_from_tickets(
        [
            {
                "action": "ENTRY",
                "symbol": "NVDA",
                "right": "C",
                "ask": 1.2,
                "contract": "NVDA261002C00190000",
                "expiry": "2026-10-02",
                "strike": 190,
                "ensemble_score": 70,
                "volume": 500,
            }
        ],
        max_consecutive_losses=3,
        max_cash_frac=0.30,
        min_ask=0.20,
        max_ask=2.50,
    )
    assert sync.get("paused") is True
    assert sync.get("entered") == []

    tr2 = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="2026-10-01-500k-sprint",
        rebuild_seed_usd=1000,
        rebuild_reason="$500k sprint",
    )
    assert tr2.book.cash == 1000
    assert tr2.book.losses == 0
    assert tr2.consecutive_losses() == 0
    for sym in ("LUNR", "QUBT", "GOOGL"):
        assert sym in tr2.recent_loss_symbols(cooldown_days=7)
    sync2 = tr2.sync_from_tickets(
        [
            {
                "action": "BUY_RIP",
                "symbol": "NVDA",
                "right": "C",
                "ask": 1.2,
                "contract": "NVDA261002C00190000",
                "expiry": "2026-10-02",
                "strike": 190,
                "ensemble_score": 58,
                "volume": 800,
            }
        ],
        max_consecutive_losses=3,
        max_cash_frac=0.30,
        min_ask=0.20,
        max_ask=2.50,
        prefer_core_megas=True,
        min_ensemble=60,
        mega_min_ensemble=55,
    )
    assert sync2.get("paused") is not True
    assert sync2.get("entered")
    assert tr2.open_trades()[0].symbol == "NVDA"


def test_time_boxed_path_1mo_500k_sprint():
    pace = time_boxed_path(
        start_usd=1000,
        milestone_usd=500_000,
        target_usd=1_000_000,
        months=1.0,
        ideal_hold_days=1,
    )
    assert pace["flips_in_window"] >= 25
    assert pace["milestone"]["pct_per_flip"] < 50  # ~+40%/flip path is feasible
    assert pace["feasible"] is True
    assert pace["schedule"][-1]["hit_milestone"] is True


def test_index_sniper_radar_hot_spy_quick_inout(tmp_path):
    """SPY/QQQ RADAR HOT should sniper-enter and bank +25% fast (not hist midcaps)."""
    ledger = tmp_path / "ch.json"
    tr = ChallengeTracker(
        ledger,
        starting_cash=1000,
        epoch="2026-10-01-500k-sprint",
        rebuild_seed_usd=1000,
    )
    hot = {
        "action": "RADAR_HOT",
        "symbol": "SPY",
        "right": None,  # radar often leaves blank — infer from OCC
        "ask": 0.34,
        "bid": 0.33,
        "contract": "SPY261001C00763000",
        "expiry": "2026-10-01",
        "strike": 763,
        "dte": 0,
        "volume": 211681,
        "spot": 760.36,
        "detail": "RADAR HOT SPY wing",
    }
    sync = tr.sync_from_tickets(
        [hot],
        max_cash_frac=0.30,
        min_ask=0.20,
        max_ask=2.50,
        prefer_core_megas=True,
        min_ensemble=60,
        mega_min_ensemble=55,
    )
    assert sync.get("paused") is not True
    assert sync.get("entered")
    open_t = tr.open_trades()[0]
    assert open_t.symbol == "SPY"
    assert open_t.right == "C"
    assert open_t.certainty_tier == "sniper"
    assert open_t.cost == 34.0
    # Bank +25% sniper exit
    open_t.entered_at = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    tr.save()
    ev = tr.evaluate_open(open_t, mark=0.43, quote={}, sprint_desk=True)  # +26.5%
    assert ev["action"] == "EXIT"
    assert "sniper" in ev["detail"].lower()


def test_spx_maps_to_spy_sniper(tmp_path):
    tr = ChallengeTracker(tmp_path / "ch.json", starting_cash=1000)
    entered = tr.enter(
        {
            "action": "SNIPER",
            "symbol": "SPX",
            "right": "C",
            "ask": 0.55,
            "contract": "SPY261001C00762000",
            "expiry": "2026-10-01",
            "strike": 762,
            "dte": 0,
            "volume": 1000,
            "spot": 760,
        },
        max_cash_frac=0.30,
        min_ask=0.20,
        max_ask=2.50,
    )
    assert entered is not None
    assert entered.symbol == "SPY"


def test_challenge_board_force_includes_index_snipers():
    board = build_challenge_board(
        win_table={"symbols": {}},
        scores=[
            {"symbol": "SPY", "horizon": "0dte", "ensemble_score": 44, "quality": False, "last_price": 760},
            {"symbol": "QQQ", "horizon": "0dte", "ensemble_score": 55, "quality": False, "last_price": 738},
        ],
        quotes={
            "SPY": {"last": 760, "session_change_pct": -0.4},
            "QQQ": {"last": 738, "session_change_pct": -0.5},
        },
        fetch_contracts=False,
        fetch_earnings=False,
        sprint_desk=True,
        max_tickets=8,
        flips=30,
        pace_months=1.0,
        pace_milestone_usd=500_000,
    )
    syms = [t["symbol"] for t in board["tickets"]]
    assert "SPY" in syms
    assert "QQQ" in syms
    assert any("sniper" in (r or "").lower() for r in (board.get("rules") or []))
