from odte_scanner.signals.actions import (
    ActionSignal,
    apply_hist_win_gate,
    build_action_board,
    decide_entry,
    decide_exit,
)
from datetime import datetime
from zoneinfo import ZoneInfo

_MORNING = datetime(2026, 8, 11, 11, 0, tzinfo=ZoneInfo("America/New_York"))


def test_scan_ask_not_stale_blocks_buy():
    """Regression: UI was forcing quote_stale=True → empty live BUY NOW."""
    sig = decide_entry(
        {
            "symbol": "COST",
            "score": 68,
            "strike": 950,
            "expiry": "2026-08-28",
            "ask": 8.5,
            "bid": 8.2,
            "contract": "COST260828C00950000",
            "dte": 7,
            "dte_bucket": "weekly",
            "quote_stale": False,
        },
        quote={
            "last": 949,
            "session_change_pct": 1.55,
            "mom_5m_pct": 0.11,
            "mom_15m_pct": 0.27,
        },
        buy_score=72,
        weekly_buy_score=65,
        require_live_confirm=True,
        now=_MORNING,
    )
    assert sig.action == "BUY_NOW"


def test_stale_without_ask_still_waits():
    sig = decide_entry(
        {
            "symbol": "COST",
            "score": 80,
            "strike": 950,
            "expiry": "2026-08-28",
            "ask": None,
            "contract": "COST260828C00950000",
            "dte_bucket": "weekly",
            "quote_stale": True,
        },
        quote={"last": 949, "session_change_pct": 1.0, "mom_5m_pct": 0.2},
        buy_score=72,
        require_live_confirm=True,
        now=_MORNING,
    )
    assert sig.action == "WAIT"
    assert "stale" in sig.detail.lower() or "blind" in sig.detail.lower()


def test_hood_chase_waits_until_pullback_reclaim():
    """HOOD +11% with weak 5m must WAIT; weekly reclaim with 5m bounce can BUY."""
    cand = {
        "symbol": "HOOD",
        "score": 75,
        "strike": 105,
        "expiry": "2026-08-28",
        "ask": 4.0,
        "bid": 3.8,
        "contract": "HOOD260828C00105000",
        "dte": 7,
        "dte_bucket": "weekly",
    }
    blocked = decide_entry(
        cand,
        quote={
            "last": 106,
            "session_change_pct": 11.3,
            "mom_5m_pct": -0.13,
            "mom_15m_pct": -0.18,
        },
        buy_score=72,
        weekly_buy_score=65,
        max_chase_pct=2.5,
        now=_MORNING,
    )
    assert blocked.action == "WAIT"
    assert "chase" in blocked.detail.lower() or "pullback" in blocked.detail.lower()

    reclaim = decide_entry(
        cand,
        quote={
            "last": 104,
            "session_change_pct": 9.5,
            "mom_5m_pct": 0.12,
            "mom_15m_pct": 0.05,
        },
        buy_score=72,
        weekly_buy_score=65,
        max_chase_pct=2.5,
        now=_MORNING,
    )
    assert reclaim.action == "BUY_NOW"
    assert "pullback reclaim" in reclaim.detail.lower()


def test_grind_continuation_lifts_cost_class():
    from odte_scanner.algos.signals import grind_continuation
    import pandas as pd
    import numpy as np

    idx = pd.date_range("2026-01-01", periods=80, freq="B")
    # Slow grind above rising MAs
    close = pd.Series(np.linspace(900, 948, len(idx)), index=idx)
    close.iloc[-1] = 949.0
    close.iloc[-2] = 934.0
    high = close * 1.005
    low = close * 0.995
    df = pd.DataFrame({"Open": close.shift(1).fillna(close), "High": high, "Low": low, "Close": close, "Volume": 1e6})
    sig = grind_continuation(df)
    assert sig.bullish
    assert sig.score >= 65

    # Parabolic rip must not look like a grind
    close2 = close.copy()
    close2.iloc[-2] = 90.0
    close2.iloc[-1] = 106.0
    high2 = close2 * 1.01
    df2 = pd.DataFrame({"Open": close2.shift(1).fillna(close2), "High": high2, "Low": close2 * 0.99, "Close": close2, "Volume": 1e6})
    rip = grind_continuation(df2)
    assert rip.score < 50
    assert not rip.bullish


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
        now=_MORNING,
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
        now=_MORNING,
    )
    assert sig.action == "WAIT"
    assert "724" in sig.detail or "weak" in sig.detail.lower() or "below" in sig.detail.lower() or "melting" in sig.detail.lower() or "bounce" in sig.detail.lower() or "high" in sig.detail.lower()


def test_wait_on_soft_tape():
    sig = decide_entry(
        {"symbol": "MU", "score": 75, "strike": 880, "expiry": "2026-08-05", "ask": 4.0, "contract": "X", "dte_bucket": "0dte"},
        quote={"last": 876, "session_change_pct": -1.8, "change_pct": 5.0, "mom_5m_pct": -0.2},
        buy_score=70,
        now=_MORNING,
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


def test_sell_now_take_profit_and_stop():
    tp = decide_exit(
        {
            "symbol": "QQQ",
            "status": "open",
            "entry": 2.0,
            "mark": 3.8,
            "bid": 3.7,
            "contract": "QQQC",
            "id": "tp1",
            "score": 70,
        },
        quote={"last": 500, "session_change_pct": 0.2, "mom_5m_pct": 0.05},
        score_by_symbol={"QQQ": 72},
        take_profit_pct=80.0,
        stop_loss_pct=50.0,
        sell_score=48,
    )
    assert tp is not None
    assert tp.action == "SELL_NOW"
    assert tp.bid == 3.7
    assert "take profit" in tp.detail.lower()

    sl = decide_exit(
        {
            "symbol": "IWM",
            "status": "open",
            "entry_ask": 2.0,
            "mark": 0.9,
            "bid": 0.85,
            "contract": "IWMC",
            "id": "sl1",
            "score": 70,
        },
        quote={"last": 220, "session_change_pct": -0.2, "mom_5m_pct": 0.0},
        score_by_symbol={"IWM": 65},
        take_profit_pct=80.0,
        stop_loss_pct=50.0,
        sell_score=48,
    )
    assert sl is not None
    assert sl.action == "SELL_NOW"
    assert sl.bid == 0.85
    assert "stop" in sl.detail.lower()


def test_action_board_uses_journal_opens_for_exit():
    board = build_action_board(
        candidates=[],
        scores=[{"symbol": "SPY", "ensemble_score": 70}],
        quotes={"SPY": {"last": 770, "session_change_pct": -1.5, "mom_5m_pct": -0.5}},
        ledger=None,
        journal_opens=[
            {
                "symbol": "SPY",
                "status": "open",
                "entry_ask": 1.5,
                "mark": 1.1,
                "bid": 1.05,
                "contract": "SPY260811C00770000",
                "id": "j1",
                "dte_bucket": "0dte",
            }
        ],
        sell_score=48,
        stop_loss_pct=50,
        take_profit_pct=80,
        require_hist_win=False,
    )
    assert board["counts"]["sell_now"] >= 1
    sell = board["sell_now"][0]
    assert sell["symbol"] == "SPY"
    assert sell["bid"] == 1.05
    assert sell["ask"] == 1.05  # not entry 1.5


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
        require_hist_win=False,
        now=_MORNING,
    )
    assert board["primary"]["action"] == "SELL_NOW"
    assert board["counts"]["sell_now"] >= 1


def test_hist_win_gate_blocks_sub_80():
    sig = ActionSignal(
        action="BUY_NOW",
        strength=80,
        headline="BUY NOW QQQ · 0DTE",
        detail="score ok",
        symbol="QQQ",
        win_pct=55.0,
        win_samples=12,
        dte_bucket="0dte",
    )
    out = apply_hist_win_gate(sig, min_hist_win_pct=80, min_hist_win_samples=5)
    assert out.action == "WAIT"
    assert "blocked" in out.detail


def test_hist_win_gate_allows_80_plus():
    sig = ActionSignal(
        action="BUY_NOW",
        strength=80,
        headline="BUY NOW MSFT · 0DTE",
        detail="score ok",
        symbol="MSFT",
        win_pct=85.7,
        win_samples=7,
        dte_bucket="0dte",
    )
    out = apply_hist_win_gate(sig, min_hist_win_pct=80, min_hist_win_samples=5)
    assert out.action == "BUY_NOW"


def test_board_requires_80_hist_win_for_buy():
    win_table = {
        "symbols": {
            "QQQ": {"0dte": {"win_pct": 55.0, "trades": 10, "wins": 5, "hit_1pct": 20.0}},
            "MSFT": {"0dte": {"win_pct": 85.7, "trades": 7, "wins": 6, "hit_1pct": 71.4}},
        }
    }
    board = build_action_board(
        candidates=[
            {
                "symbol": "QQQ",
                "score": 80,
                "strike": 720,
                "expiry": "2026-08-05",
                "ask": 2.0,
                "bid": 1.9,
                "contract": "QQQ1",
                "dte": 0,
                "dte_bucket": "0dte",
            },
            {
                "symbol": "MSFT",
                "score": 80,
                "strike": 420,
                "expiry": "2026-08-05",
                "ask": 2.5,
                "bid": 2.4,
                "contract": "MSFT1",
                "dte": 0,
                "dte_bucket": "0dte",
            },
        ],
        scores=[
            {"symbol": "QQQ", "ensemble_score": 80},
            {"symbol": "MSFT", "ensemble_score": 80},
        ],
        quotes={
            "QQQ": {
                "last": 721,
                "session_change_pct": 0.2,
                "mom_5m_pct": 0.15,
                "mom_15m_pct": 0.2,
                "dist_from_day_high_pct": -0.05,
            },
            "MSFT": {
                "last": 421,
                "session_change_pct": 0.3,
                "mom_5m_pct": 0.12,
                "mom_15m_pct": 0.18,
                "dist_from_day_high_pct": -0.08,
            },
        },
        ledger=None,
        buy_score=70,
        win_rate_table=win_table,
        require_hist_win=True,
        min_hist_win_pct=80,
        min_hist_win_samples=5,
        now=_MORNING,
    )
    buy_syms = {b["symbol"] for b in board["buy_now"]}
    assert "MSFT" in buy_syms
    assert "QQQ" not in buy_syms
    assert board["hist_win_gate"]["target_met"] is True
    assert board["hist_win_gate"]["pooled_win_pct"] >= 80


def test_put_blocked_on_rally_without_tape():
    """Regression: AMZN put promoted on Pages without tape / into green session."""
    put = {
        "symbol": "AMZN",
        "score": 72,
        "put_score": 72,
        "strike": 230,
        "expiry": "2026-08-28",
        "ask": 3.15,
        "bid": 3.0,
        "contract": "AMZN260828P00230000",
        "dte": 0,
        "dte_bucket": "0dte",
        "right": "P",
        "moneyness_pct": 0.1,
    }
    rally = decide_entry(
        put,
        quote={
            "last": 232,
            "session_change_pct": 1.8,
            "mom_5m_pct": 0.35,
            "mom_15m_pct": 0.42,
        },
        buy_score=70,
        require_live_confirm=False,
        now=_MORNING,
    )
    assert rally.action == "WAIT"

    blind = decide_entry(
        put,
        quote={},
        buy_score=70,
        require_live_confirm=False,
        now=_MORNING,
    )
    assert blind.action == "WAIT"
    assert "tape" in blind.detail.lower() or "blind" in blind.detail.lower()


def test_put_hist_gate_blocks_bullish_underlying():
    sig = ActionSignal(
        action="BUY_NOW",
        strength=80,
        headline="BUY NOW AMZN PUT · 0DTE",
        detail="score ok",
        symbol="AMZN",
        right="P",
        win_pct=72.7,
        win_samples=11,
        hit_1pct=65.0,
        dte_bucket="0dte",
    )
    out = apply_hist_win_gate(sig, min_hist_win_pct=80, min_hist_win_samples=5)
    assert out.action == "WAIT"
    assert "put edge" in out.detail or "rips" in out.detail
