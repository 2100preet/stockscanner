"""Backtest-style regression for paper journal exit P&L (live-ask / mark / reprice).

Mirrors production bugs: SELL reason cited live ask while exit_bid stayed at entry.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from odte_scanner.signals.actions import build_action_board, decide_exit
from odte_scanner.signals.lottery import build_lottery_board, decide_lottery_exit
from odte_scanner.trading.journal import SignalJournal, _parse_live_exit_from_reason

ET = ZoneInfo("America/New_York")
_SESSION = datetime(2026, 8, 12, 11, 0, tzinfo=ET)
_LATE = datetime(2026, 8, 12, 15, 50, tzinfo=ET)

# Closed trades that were flat on live Pages before the fix (subset with live-ask reasons)
_LIVE_FLAT_CASES = [
    ("AMZN", 0.81, 0.26, "P"),
    ("AVGO", 2.00, 1.20, "P"),
    ("INTC", 1.02, 0.57, "P"),
    ("IWM", 0.86, 0.41, "P"),
    ("IWM", 0.80, 0.29, "P"),
    ("AMD", 2.04, 1.05, "C"),
]


def _enter(
    j: SignalJournal,
    symbol: str,
    entry: float,
    *,
    right: str = "P",
    contract: str | None = None,
) -> None:
    t = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": symbol,
            "contract": contract or f"{symbol}260812{'P' if right == 'P' else 'C'}00100000",
            "expiry": "2026-08-12",
            "strike": 100,
            "ask": entry,
            "score": 72,
            "dte_bucket": "0dte",
            "detail": f"buy {symbol}",
            "right": right,
        },
        max_per_day=20,
    )
    assert t is not None, f"enter failed for {symbol} @ {entry}"


def _enter_put(j: SignalJournal, symbol: str, entry: float, *, contract: str | None = None) -> None:
    _enter(j, symbol, entry, right="P", contract=contract)


def test_parse_live_ask_variants():
    assert _parse_live_exit_from_reason("live ask $0.26 vs entry $0.81 (−40%+)") == 0.26
    assert _parse_live_exit_from_reason("foo; live ask $1.20 vs entry $2.00 (−40%+)") == 1.20
    assert _parse_live_exit_from_reason("unreal -48% @ $1.05") == 1.05
    assert _parse_live_exit_from_reason("time-stop only") is None


@pytest.mark.parametrize("symbol,entry,exit_px,right", _LIVE_FLAT_CASES)
def test_live_flat_case_exit_from_signal_not_zero(tmp_path, symbol, entry, exit_px, right):
    """Each production flat exit with live-ask reason must book non-zero P&L."""
    j = SignalJournal(tmp_path / f"{symbol}.json", starting_cash=5000)
    j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": symbol,
            "contract": f"{symbol}260812{'P' if right == 'P' else 'C'}00100000",
            "expiry": "2026-08-12",
            "strike": 100,
            "ask": entry,
            "score": 72,
            "dte_bucket": "0dte",
            "detail": "buy",
            "right": right,
        }
    )
    reason = f"live ask ${exit_px:.2f} vs entry ${entry:.2f} (−40%+)"
    closed = j.exit_from_signal(
        {
            "action": "SELL_NOW",
            "symbol": symbol,
            "bid": entry,  # stale echo
            "ask": entry,
            "detail": reason,
        }
    )
    assert len(closed) == 1
    t = closed[0]
    assert t.exit_bid == pytest.approx(exit_px)
    assert t.pnl_usd == pytest.approx((exit_px - entry) * 100)
    assert t.profit_pct == pytest.approx(((exit_px - entry) / entry) * 100, rel=0, abs=0.1)
    assert t.pnl_usd != 0


def test_batch_reprice_matches_live_journal_totals(tmp_path):
    """Simulate Pages load: 6 known flat exits repriced → cash matches realized P&L."""
    j = SignalJournal(tmp_path / "batch.json", starting_cash=5000)
    expected_realized = 0.0
    for symbol, entry, exit_px, right in _LIVE_FLAT_CASES:
        _enter(j, symbol, entry, right=right)
        tid = j.book.trades[-1].id
        j.exit_trade(
            tid,
            exit_bid=entry,
            reason=f"live ask ${exit_px:.2f} vs entry ${entry:.2f} (−40%+)",
        )
        expected_realized += (exit_px - entry) * 100

    assert all(t.pnl_usd == 0 for t in j.book.trades)
    n = j.reprice_flat_exits_from_reasons()
    assert n == len(_LIVE_FLAT_CASES)
    perf = j.performance()
    assert perf["realized_pnl_usd"] == pytest.approx(expected_realized, abs=0.01)
    assert j.book.cash == pytest.approx(5000 + expected_realized, abs=0.01)
    assert perf["cash"] == pytest.approx(j.book.cash, abs=0.01)


def test_reprice_is_idempotent(tmp_path):
    j = SignalJournal(tmp_path / "idem.json", starting_cash=5000)
    _enter_put(j, "AMZN", 0.81)
    j.exit_trade(j.book.trades[0].id, exit_bid=0.81, reason="live ask $0.26 vs entry $0.81 (−40%+)")
    assert j.reprice_flat_exits_from_reasons() == 1
    cash_after = j.book.cash
    pnl_after = j.book.trades[0].pnl_usd
    assert j.reprice_flat_exits_from_reasons() == 0
    assert j.book.cash == cash_after
    assert j.book.trades[0].pnl_usd == pnl_after


def test_lottery_board_to_journal_nonzero_pnl(tmp_path):
    """Full path: open trade → lottery SELL (mark stuck) → journal sync."""
    j = SignalJournal(tmp_path / "lotto.json", starting_cash=5000)
    entered = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "MU",
            "contract": "MU260812P00100000",
            "expiry": "2026-08-12",
            "strike": 100,
            "ask": 0.81,
            "score": 70,
            "dte_bucket": "0dte",
            "detail": "lottery buy",
            "right": "P",
        }
    )
    assert entered is not None
    opens = [
        {
            **entered.to_dict(),
            "entry": 0.81,
            "mark": 0.81,  # never refreshed
            "bid": 0.81,
            "status": "open",
        }
    ]
    ticket = {
        "symbol": "MU",
        "contract": "MU260812P00100000",
        "expiry": "2026-08-12",
        "strike": 100,
        "dte": 0,
        "ask": 0.26,
        "bid": 0.20,
        "lottery_score": 75,
    }
    board = build_lottery_board(
        [ticket],
        quotes={"MU": {"last": 98, "session_change_pct": -0.2, "mom_5m_pct": 0.0, "mom_15m_pct": 0.05}},
        scores=[{"symbol": "MU", "horizon": "0dte", "ensemble_score": 70}],
        open_trades=opens,
        now=_SESSION,
    )
    assert board["counts"]["sell_now"] >= 1
    sell = board["sell_now"][0]
    assert sell["bid"] == pytest.approx(0.20)
    sync = j.sync_from_actions(
        {"sell_now": [sell], "buy_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
        auto_enter=False,
        auto_exit=True,
    )
    assert len(sync["exited"]) == 1
    ex = sync["exited"][0]
    assert ex["pnl_usd"] != 0
    assert ex["exit_bid"] == pytest.approx(0.20)
    assert ex["profit_pct"] == pytest.approx(((0.20 - 0.81) / 0.81) * 100, abs=0.2)


def test_actions_stop_loss_board_to_journal(tmp_path):
    """0DTE desk decide_exit → sync_from_actions with melted mark."""
    j = SignalJournal(tmp_path / "desk.json", starting_cash=5000)
    entered = j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "SPY",
            "contract": "SPY260812C00560000",
            "expiry": "2026-08-12",
            "strike": 560,
            "ask": 2.50,
            "score": 80,
            "dte_bucket": "0dte",
            "detail": "buy spy",
        }
    )
    j.mark_open({"SPY260812C00560000": 1.00})
    opens = [
        {
            **entered.to_dict(),
            "entry": 2.50,
            "mark": 1.00,
            "bid": 1.00,
            "status": "open",
        }
    ]
    board = build_action_board(
        candidates=[],
        scores=[{"symbol": "SPY", "ensemble_score": 70}],
        quotes={"SPY": {"last": 558, "session_change_pct": -0.3, "mom_5m_pct": -0.1}},
        ledger=None,
        journal_opens=opens,
        stop_loss_pct=50,
        take_profit_pct=80,
        sell_score=48,
        require_hist_win=False,
        now=_SESSION,
    )
    assert board["counts"]["sell_now"] >= 1
    sell = board["sell_now"][0]
    assert sell["bid"] == 1.0
    sync = j.sync_from_actions(
        {"sell_now": [sell], "buy_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
        auto_enter=False,
        auto_exit=True,
    )
    ex = sync["exited"][0]
    assert ex["pnl_usd"] == pytest.approx(-150.0)
    assert ex["profit_pct"] == pytest.approx(-60.0)


def test_take_profit_winner_pnl(tmp_path):
    j = SignalJournal(tmp_path / "win.json", starting_cash=5000)
    j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "NVDA",
            "contract": "NVDA260812C00100000",
            "expiry": "2026-08-12",
            "strike": 100,
            "ask": 1.0,
            "score": 75,
            "dte_bucket": "0dte",
            "detail": "buy",
        }
    )
    j.mark_open({"NVDA260812C00100000": 2.5})
    sig = decide_exit(
        {
            "symbol": "NVDA",
            "status": "open",
            "entry_ask": 1.0,
            "mark": 2.5,
            "bid": 2.4,
            "contract": "NVDA260812C00100000",
            "id": j.book.trades[0].id,
            "dte_bucket": "0dte",
        },
        quote={"last": 105, "session_change_pct": 1.5, "mom_5m_pct": 0.3},
        score_by_symbol={"NVDA": 75},
        take_profit_pct=80,
        stop_loss_pct=50,
    )
    assert sig is not None and sig.action == "SELL_NOW"
    closed = j.exit_from_signal(sig.to_dict())
    assert closed[0].pnl_usd == pytest.approx(140.0)  # (2.4-1.0)*100
    assert closed[0].profit_pct == pytest.approx(140.0)
    assert j.performance()["win_rate_pct"] == 100.0


def test_clock_exit_without_live_mark_stays_flat(tmp_path):
    """Time-stop with no live mark still exits at entry (documented limitation)."""
    j = SignalJournal(tmp_path / "clock.json", starting_cash=5000)
    j.enter_from_signal(
        {
            "action": "BUY_NOW",
            "symbol": "SPY",
            "contract": "SPY260812C00560000",
            "expiry": "2026-08-12",
            "strike": 560,
            "ask": 1.5,
            "score": 80,
            "dte_bucket": "0dte",
            "detail": "buy",
        }
    )
    opens = [
        {
            **j.book.trades[0].to_dict(),
            "entry": 1.5,
            "mark": 1.5,
            "bid": 1.5,
            "status": "open",
        }
    ]
    board = build_action_board(
        candidates=[],
        scores=[{"symbol": "SPY", "ensemble_score": 70}],
        quotes={"SPY": {"last": 561, "session_change_pct": 0.1, "mom_5m_pct": 0.0}},
        ledger=None,
        journal_opens=opens,
        require_hist_win=False,
        now=_LATE,
    )
    assert board["counts"]["sell_now"] >= 1
    sync = j.sync_from_actions(
        {"sell_now": board["sell_now"], "buy_now": [], "buy_now_0dte": [], "buy_now_weekly": []},
        auto_enter=False,
        auto_exit=True,
    )
    ex = sync["exited"][0]
    assert "time-stop" in ex["exit_reason"].lower() or "15:45" in ex["exit_reason"]
    assert ex["pnl_usd"] == 0.0  # no live mark available — expected


def test_mixed_session_cash_integrity(tmp_path):
    """Enter multiple, exit winners/losers/repriced — cash equals starting + realized."""
    j = SignalJournal(tmp_path / "mixed.json", starting_cash=5000)
    # Winner
    j.enter_from_signal(
        {"action": "BUY_NOW", "symbol": "QQQ", "contract": "Q1", "expiry": "2026-08-12", "strike": 400, "ask": 1.0, "score": 80, "dte_bucket": "0dte", "detail": "b"}
    )
    j.exit_from_signal({"action": "SELL_NOW", "symbol": "QQQ", "bid": 1.8, "detail": "tp"})
    # Loser via live ask reason
    _enter_put(j, "AMZN", 0.81)
    j.exit_trade(j.book.trades[-1].id, exit_bid=0.81, reason="live ask $0.26 vs entry $0.81 (−40%+)")
    j.reprice_flat_exits_from_reasons()
    # Still open
    _enter_put(j, "ASTS", 1.78, contract="ASTS260812P00020000")
    perf = j.performance()
    closed_pnl = sum(t.pnl_usd or 0 for t in j.book.trades if t.status == "closed")
    open_cost = sum(t.cost for t in j.book.trades if t.status == "open")
    assert j.book.cash == pytest.approx(5000 + closed_pnl - open_cost, abs=0.01)
    assert perf["realized_pnl_usd"] == pytest.approx(closed_pnl, abs=0.01)
    assert perf["open_trades"] == 1
    assert perf["closed_trades"] == 2
