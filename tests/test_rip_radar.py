"""Tests for META-class RIP / CONTINUATION lane + cooldown override."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.signals.actions import decide_entry
from odte_scanner.signals.rip_radar import (
    DESK_SPECIAL_EYE,
    build_rip_board,
    decide_rip_entry,
    is_desk_special_eye,
    is_mega_rip_symbol,
    mega_rip_tape_ok,
)

_MORNING = datetime(2026, 9, 18, 11, 0, tzinfo=ZoneInfo("America/New_York"))


def test_mega_symbols_include_baba_googl_amd_meta():
    for s in ("BABA", "GOOGL", "AMD", "META", "NVDA"):
        assert is_mega_rip_symbol(s)


def test_desk_special_eye_names():
    """Standing megas + premarket catalysts stay on special eye + mega RIP."""
    must = {
        "HOOD",
        "MSFT",
        "INTC",
        "PLTR",
        "AMZN",
        "MU",
        "AVGO",
        "GOOGL",
        "AMD",
        "WOLF",
        "HAE",
        "PEP",
        "XOM",
        "CVX",
        "LEVI",
        "APLD",
    }
    assert must <= set(DESK_SPECIAL_EYE)
    for s in must:
        assert is_desk_special_eye(s)
        assert is_mega_rip_symbol(s)
    # HOOD was the prior gap — must not fall off mega early / RIP again
    assert is_mega_rip_symbol("HOOD")


def test_mega_rip_tape_ok():
    assert mega_rip_tape_ok(live=1.4, mom5=0.12)
    assert not mega_rip_tape_ok(live=0.4, mom5=0.12)
    assert not mega_rip_tape_ok(live=1.4, mom5=-0.1)
    # Pages offline: session rip alone is enough when 5m/15m missing
    assert mega_rip_tape_ok(live=2.9, mom5=None, mom15=None)
    # Day-low reclaim while still red vs prior close (TSLA 347→351)
    assert mega_rip_tape_ok(
        live=-0.5, mom5=None, mom15=None, bounce_from_low=1.3
    )
    assert not mega_rip_tape_ok(
        live=-0.5, mom5=None, mom15=None, bounce_from_low=0.8
    )


def test_tsla_day_low_reclaim_alerts_without_green_session():
    """Regression: TSLA bounce 346→351 must WATCH/BUY RIP even if session still <1%."""
    act = decide_rip_entry(
        {
            "symbol": "TSLA",
            "ask": 3.5,
            "bid": 3.3,
            "strike": 350,
            "expiry": "2026-10-02",
            "contract": "TSLA261002C00350000",
            "dte": 2,
            "dte_bucket": "weekly",
            "moneyness_pct": -0.3,
            "volume": 900,
            "open_interest": 2000,
            "score": 48,
        },
        quote={
            "last": 351.0,
            "session_change_pct": -0.52,
            "mom_5m_pct": None,
            "mom_15m_pct": None,
            "day_low": 345.88,
            "day_high": 355.0,
            "prev_close": 352.84,
        },
        now=_MORNING,
    )
    assert act.action in {"BUY_RIP", "WATCH_RIP"}
    assert "day-low" in act.detail.lower() or "off day-low" in act.detail.lower()


def test_intc_session_rip_without_mom_bars():
    """Regression: INTC +3% session must not die on RIP_COOL when mom bars are None."""
    act = decide_rip_entry(
        {
            "symbol": "INTC",
            "ask": 2.72,
            "bid": 2.64,
            "strike": 120,
            "expiry": "2026-10-02",
            "contract": "INTC261002C00120000",
            "dte": 2,
            "dte_bucket": "weekly",
            "moneyness_pct": 0.3,
            "volume": 700,
            "open_interest": 1500,
            "score": 69,
            "live_change_pct": 2.95,
        },
        quote={"last": 119.65, "session_change_pct": 2.95, "mom_5m_pct": None, "mom_15m_pct": None},
        now=_MORNING,
    )
    assert act.action == "BUY_RIP"
    assert act.live_change_pct is not None and act.live_change_pct >= 1.0


def test_buy_rip_on_meta_class_continuation():
    act = decide_rip_entry(
        {
            "symbol": "META",
            "ask": 4.5,
            "bid": 4.2,
            "strike": 760,
            "expiry": "2026-09-25",
            "contract": "META260925C00760000",
            "dte": 4,
            "dte_bucket": "weekly",
            "moneyness_pct": 1.2,
            "volume": 800,
            "open_interest": 2000,
            "score": 70,
        },
        quote={"last": 752, "session_change_pct": 2.1, "mom_5m_pct": 0.18, "mom_15m_pct": 0.4},
        now=_MORNING,
    )
    assert act.action == "BUY_RIP"
    assert act.cooldown_waived is True


def test_same_occ_still_blocked_on_rip_lane():
    act = decide_rip_entry(
        {
            "symbol": "META",
            "ask": 4.5,
            "bid": 4.2,
            "strike": 682.5,
            "expiry": "2026-09-21",
            "contract": "META260921C00682500",
            "dte": 2,
            "volume": 500,
            "score": 70,
        },
        quote={"last": 700, "session_change_pct": 3.0, "mom_5m_pct": 0.2},
        loss_cooldown_contracts={"META260921C00682500"},
        now=_MORNING,
    )
    assert act.action == "RIP_COOL"
    assert "cooldown" in act.detail.lower()


def test_options_symbol_cooldown_waived_when_mega_ripping():
    """Regression: META melt-up must not stay WAIT solely for symbol cooldown."""
    cand = {
        "symbol": "META",
        "score": 70,
        "strike": 760,
        "expiry": "2026-09-25",
        "ask": 5.0,
        "bid": 4.8,
        "contract": "META260925C00760000",  # fresh OCC — not the loser
        "dte": 4,
        "dte_bucket": "weekly",
    }
    blocked = decide_entry(
        cand,
        quote={"last": 752, "session_change_pct": 0.2, "mom_5m_pct": 0.05},
        buy_score=65,
        weekly_buy_score=65,
        require_live_confirm=False,
        now=_MORNING,
        loss_cooldown_symbols={"META"},
        loss_cooldown_contracts=set(),
    )
    assert blocked.action == "WAIT"
    assert "cooldown" in blocked.detail.lower()

    ripping = decide_entry(
        cand,
        quote={"last": 752, "session_change_pct": 2.2, "mom_5m_pct": 0.15, "mom_15m_pct": 0.3},
        buy_score=65,
        weekly_buy_score=65,
        require_live_confirm=False,
        now=_MORNING,
        loss_cooldown_symbols={"META"},
        loss_cooldown_contracts=set(),
    )
    # May still WAIT for other gates, but not symbol cooldown
    assert "on loss cooldown — recent losing flip" not in ripping.detail


def test_rip_board_includes_baba_watch_without_chain():
    board = build_rip_board(
        candidates=[{"symbol": "BABA", "score": 68, "right": "C"}],
        scores=[{"symbol": "BABA", "ensemble_score": 68}],
        quotes={"BABA": {"last": 120, "session_change_pct": 1.8, "mom_5m_pct": 0.2}},
        now=_MORNING,
        signal_times_path=None,
    )
    syms = [r["symbol"] for r in (board.get("watch") or []) + (board.get("buy_rip") or [])]
    assert "BABA" in syms or any(r.get("symbol") == "BABA" for r in board.get("all") or [])


def test_rip_buy_stamps_asked_time(tmp_path):
    """Regression: AMD-style BUY RIP on Buy/Sell NOW must carry asked CST time."""
    store = tmp_path / "rip_times.json"
    board = build_rip_board(
        candidates=[
            {
                "symbol": "AMD",
                "ask": 2.2,
                "bid": 2.1,
                "strike": 630,
                "expiry": "2026-10-02",
                "contract": "AMD261002C00630000",
                "dte": 1,
                "dte_bucket": "0dte",
                "moneyness_pct": 0.5,
                "volume": 1200,
                "open_interest": 4000,
                "score": 70,
                "right": "C",
            }
        ],
        scores=[{"symbol": "AMD", "ensemble_score": 72}],
        quotes={
            "AMD": {
                "last": 635.0,
                "session_change_pct": 2.4,
                "mom_5m_pct": 0.2,
                "mom_15m_pct": 0.4,
            }
        },
        now=_MORNING,
        signal_times_path=str(store),
    )
    buys = board.get("buy_rip") or []
    assert buys, "expected AMD BUY_RIP"
    row = buys[0]
    assert row["symbol"] == "AMD"
    assert row.get("signaled_at")
    assert row.get("signaled_at_cst")
    assert "CST" in row["signaled_at_cst"] or "CDT" in row["signaled_at_cst"]
    # Sticky across rebuild
    board2 = build_rip_board(
        candidates=[
            {
                "symbol": "AMD",
                "ask": 2.3,
                "bid": 2.2,
                "strike": 630,
                "expiry": "2026-10-02",
                "contract": "AMD261002C00630000",
                "dte": 1,
                "dte_bucket": "0dte",
                "moneyness_pct": 0.5,
                "volume": 1200,
                "open_interest": 4000,
                "score": 70,
                "right": "C",
            }
        ],
        scores=[{"symbol": "AMD", "ensemble_score": 72}],
        quotes={
            "AMD": {
                "last": 636.0,
                "session_change_pct": 2.5,
                "mom_5m_pct": 0.22,
                "mom_15m_pct": 0.45,
            }
        },
        now=_MORNING,
        signal_times_path=str(store),
    )
    assert (board2.get("buy_rip") or [])[0]["signaled_at"] == row["signaled_at"]
