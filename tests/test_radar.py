"""Discord-style lottery radar — cheap SPY/QQQ wings, separate from BUY NOW."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.options.explosive import build_explosive_from_candidate
from odte_scanner.signals.radar import build_radar_board, decide_radar_entry

ET = ZoneInfo("America/New_York")
SESSION_NOW = datetime(2026, 8, 10, 10, 7, tzinfo=ET)  # ~9:07 AM CST


def _mike_ticket(**overrides):
    """SPY 776C @ $0.28 with spot ~773 — MikeInvesting-style wing."""
    base = {
        "symbol": "SPY",
        "contract": "SPY260810C00776000",
        "expiry": "2026-08-10",
        "dte": 0,
        "strike": 776,
        "spot": 773.0,
        "ask": 0.28,
        "bid": 0.26,
        "moneyness_pct": (776 - 773) / 773 * 100,
        "volume": 4500,
        "open_interest": 12000,
        "score": 59.0,
        "lottery_score": 70.0,
        "mult_at_1pct": 8.0,
        "mult_at_2pct": 16.0,
        "mult_at_3pct": 28.0,
        "best_mult": 28.0,
    }
    base.update(overrides)
    return base


def _quote(**overrides):
    base = {
        "last": 773.4,
        "session_change_pct": 0.22,
        "change_pct": 0.22,
        "mom_5m_pct": 0.09,
        "mom_15m_pct": 0.12,
        "dist_from_day_high_pct": -0.18,
    }
    base.update(overrides)
    return base


def test_mike_style_wing_is_radar_hot_not_buy_now():
    from odte_scanner.signals.lottery import decide_lottery_entry

    ticket = _mike_ticket(lottery_score=50)  # below BUY NOW lottery floor
    quote = _quote()
    radar = decide_radar_entry(ticket, quote=quote, ensemble_score=59, now=SESSION_NOW)
    assert radar.action == "RADAR_HOT"
    assert radar.ask == 0.28
    assert radar.confirms >= 3

    # Strict lottery BUY NOW still rejects — score floor / gates unchanged
    lot = decide_lottery_entry(ticket, quote=quote, ensemble_score=59, now=SESSION_NOW)
    assert lot.action != "BUY_NOW"


def test_radar_cool_when_far_otm():
    sig = decide_radar_entry(
        _mike_ticket(strike=790, moneyness_pct=2.2, spot=773),
        quote=_quote(),
        ensemble_score=59,
        now=SESSION_NOW,
    )
    assert sig.action == "RADAR_COOL"


def test_radar_cool_on_too_rich_ask():
    sig = decide_radar_entry(
        _mike_ticket(ask=4.50),
        quote=_quote(),
        ensemble_score=70,
        now=SESSION_NOW,
    )
    assert sig.action == "RADAR_COOL"


def test_cheap_ask_builds_explosive_candidate():
    ec = build_explosive_from_candidate(
        _mike_ticket(),
        min_best_mult=2.2,
        min_mult_at_3pct=1.8,
        min_mult_at_1pct=1.5,
    )
    assert ec is not None
    assert ec.ask == 0.28
    assert ec.mult_at_1pct >= 1.5


def test_build_radar_board_separates_lanes():
    board = build_radar_board(
        [_mike_ticket(), _mike_ticket(symbol="QQQ", contract="QQQ260810C00580000", strike=580, spot=578, ask=0.35)],
        quotes={"SPY": _quote(), "QQQ": _quote(last=578)},
        scores=[{"symbol": "SPY", "ensemble_score": 59, "horizon": "0dte"}],
        now=SESSION_NOW,
    )
    assert board["lane"] == "radar"
    assert "BUY_NOW" not in str(board.get("hot"))
    assert board["counts"]["hot"] + board["counts"]["watch"] + board["counts"]["cool"] >= 1
    assert "journal" in (board.get("note") or "").lower() or "BUY NOW" in (board.get("note") or "")
