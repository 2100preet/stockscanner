"""Tests for beauty/monthly lane + ChallengeBook.equity fix + Oct-end pace."""
from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.challenge.tracker import ChallengeBook, ChallengeTracker
from odte_scanner.signals.beauty_monthly import (
    build_beauty_board,
    decide_beauty_entry,
    is_beauty_symbol,
    oct_end_pace_note,
)

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo("America/New_York"))


def test_beauty_symbols_include_sep_names():
    for s in ("AMD", "META", "MU", "SNDK"):
        assert is_beauty_symbol(s)


def test_challenge_book_equity_property():
    """Regression: Pages crashed with ChallengeBook has no attribute equity."""
    book = ChallengeBook(starting_cash=1000, cash=725.0)
    assert book.equity == 725.0
    d = book.to_dict()
    assert d["equity"] == 725.0


def test_oct_end_pace_from_1k():
    pace = oct_end_pace_note(equity=1000, deadline="2026-10-31", now=_NOW)
    assert pace["days_left"] >= 30
    assert pace["flips_in_window"] >= 15
    assert pace["pct_per_flip"] > 40
    assert "Oct-end" in pace["note"]


def test_buy_beauty_monthly_dte():
    act = decide_beauty_entry(
        {
            "symbol": "AMD",
            "ask": 18.0,
            "bid": 17.5,
            "strike": 620,
            "expiry": "2026-10-17",
            "contract": "AMD261017C00620000",
            "dte": 24,
            "moneyness_pct": 1.5,
            "volume": 400,
            "open_interest": 1200,
            "score": 72,
            "month_change_pct": 22.0,
        },
        quote={"last": 610, "session_change_pct": 0.8, "month_change_pct": 22.0},
        now=_NOW,
    )
    assert act.action == "BUY_BEAUTY"


def test_beauty_rejects_0dte():
    act = decide_beauty_entry(
        {
            "symbol": "META",
            "ask": 5.0,
            "bid": 4.8,
            "strike": 750,
            "expiry": "2026-09-23",
            "contract": "META260923C00750000",
            "dte": 0,
            "volume": 900,
            "score": 70,
            "month_change_pct": 18.0,
        },
        quote={"last": 740, "session_change_pct": 1.2, "month_change_pct": 18.0},
        now=_NOW,
    )
    assert act.action in {"WATCH_BEAUTY", "BEAUTY_COOL"}


def test_beauty_board_lists_sndk():
    board = build_beauty_board(
        candidates=[
            {
                "symbol": "SNDK",
                "score": 68,
                "ask": 22.0,
                "dte": 28,
                "strike": 1800,
                "expiry": "2026-10-17",
                "contract": "SNDK261017C01800000",
                "month_change_pct": 12.0,
                "volume": 200,
                "right": "C",
            }
        ],
        scores=[{"symbol": "SNDK", "ensemble_score": 68}],
        quotes={"SNDK": {"last": 1760, "session_change_pct": 0.5, "month_change_pct": 12.0}},
        now=_NOW,
    )
    buys = board.get("buy_beauty") or []
    assert any(r["symbol"] == "SNDK" for r in buys) or any(
        r["symbol"] == "SNDK" for r in (board.get("watch") or [])
    )


def test_tracker_equity_accessible(tmp_path):
    tr = ChallengeTracker(tmp_path / "ch.json", starting_cash=1000)
    assert tr.book.equity == 1000.0
