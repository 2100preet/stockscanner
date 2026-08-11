"""End-to-end: journal enter → mark → SELL NOW → exit with real P&L + timestamps."""
from __future__ import annotations

from odte_scanner.config import load_config
from odte_scanner.data.universe import FOCUS_DEFAULT, earnings_darlings_universe, liquid_universe
from odte_scanner.signals.actions import build_action_board, decide_exit
from odte_scanner.trading.journal import SignalJournal


def test_user_named_tickers_are_scannable():
    """NBIS/CRWV/CRCL/NOW/CRM must be in the desk universe (not missing from lists)."""
    liq = set(liquid_universe())
    focus = set(FOCUS_DEFAULT)
    cfg = load_config()
    cfg_tickers = {str(t).upper() for t in (cfg.get("tickers") or [])}
    for sym in ("NBIS", "CRWV", "CRCL", "NOW", "CRM", "SMCI"):
        assert sym in liq, f"{sym} missing from liquid universe"
        assert sym in focus or sym in cfg_tickers, f"{sym} missing from focus/config"
    for sym in ("NBIS", "CRWV", "CRCL", "SMCI"):
        assert sym in earnings_darlings_universe()


def test_e2e_exit_shows_time_and_pnl(tmp_path):
    j = SignalJournal(tmp_path / "e2e.json", starting_cash=5000)
    entered = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "CRWV",
            "contract": "CRWV260814C00100000",
            "expiry": "2026-08-14",
            "strike": 100,
            "ask": 2.50,
            "score": 80,
            "dte_bucket": "0dte",
            "detail": "test enter",
            "live_last": 105,
        }
    )
    assert entered is not None
    assert entered.entered_at
    assert entered.cash_after == 5000 - 250

    # Premium melts → stop
    j.mark_open({"CRWV260814C00100000": 1.00})
    board = build_action_board(
        candidates=[],
        scores=[{"symbol": "CRWV", "ensemble_score": 70}],
        quotes={"CRWV": {"last": 102, "session_change_pct": -0.5, "mom_5m_pct": 0.0}},
        ledger=None,
        journal_opens=[
            {
                **entered.to_dict(),
                "entry": 2.50,
                "mark": 1.00,
                "bid": 1.00,
                "status": "open",
            }
        ],
        stop_loss_pct=50,
        take_profit_pct=80,
        sell_score=48,
        require_hist_win=False,
    )
    assert board["counts"]["sell_now"] >= 1
    sell = board["sell_now"][0]
    assert sell["action"] == "SELL_NOW"
    assert sell["bid"] == 1.0
    assert sell["ask"] == 1.0

    sync = j.sync_from_actions(
        {"sell_now": [sell], "buy_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
        auto_enter=False,
        auto_exit=True,
    )
    assert len(sync["exited"]) == 1
    closed = sync["exited"][0]
    assert closed["exited_at"]
    assert closed["profit_pct"] == -60.0  # (1.0-2.5)/2.5
    assert closed["pnl_usd"] == -150.0
    assert closed["cash_after"] == 5000 - 250 + 100
    assert sync["performance"]["realized_pnl_usd"] == -150.0
    assert sync["performance"]["closed_trades"] == 1
    assert sync["performance"]["balance_log"][-1]["action"] == "EXIT"
    assert sync["performance"]["balance_log"][-1]["profit_pct"] == -60.0


def test_take_profit_exit_pnl(tmp_path):
    j = SignalJournal(tmp_path / "tp.json", starting_cash=5000)
    j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "NBIS",
            "contract": "NBIS260814C00050000",
            "expiry": "2026-08-14",
            "strike": 50,
            "ask": 1.0,
            "score": 75,
            "dte_bucket": "weekly",
            "detail": "enter",
        }
    )
    j.mark_open({"NBIS260814C00050000": 2.0})
    sig = decide_exit(
        {
            "symbol": "NBIS",
            "status": "open",
            "entry_ask": 1.0,
            "mark": 2.0,
            "bid": 1.95,
            "contract": "NBIS260814C00050000",
            "id": j.book.trades[0].id,
        },
        quote={"last": 55, "session_change_pct": 1.0, "mom_5m_pct": 0.2},
        score_by_symbol={"NBIS": 74},
        take_profit_pct=80,
        stop_loss_pct=50,
    )
    assert sig is not None and sig.action == "SELL_NOW"
    assert "take profit" in sig.detail.lower()
    closed = j.exit_from_signal(sig.to_dict())
    assert len(closed) == 1
    assert closed[0].profit_pct == 95.0  # bid 1.95
    assert closed[0].exited_at
    assert closed[0].pnl_usd == 95.0
