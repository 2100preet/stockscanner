"""Premarket movers → session special-eye."""

from __future__ import annotations

from types import SimpleNamespace

from odte_scanner.signals.premarket_movers import (
    CATALYST_SEEDS,
    effective_special_eye,
    fetch_premarket_movers,
    rank_movers,
    static_special_eye,
)
from odte_scanner.signals.rip_radar import is_mega_rip_symbol


def test_catalyst_seeds_include_today_movers():
    must = {
        "TMUS",
        "T",
        "VZ",
        "CCI",
        "AMT",
        "SBAC",
        "HUM",
        "ALHC",
        "AAPL",
        "ORCL",
        "LITE",
        "CRWV",
        "AMD",
        "AVGO",
        "NVDA",
        "SPCX",
    }
    assert must <= set(CATALYST_SEEDS)
    eye = static_special_eye({"actions": {"desk_special_eye": ["HOOD"]}})
    assert must <= eye
    for s in must:
        assert is_mega_rip_symbol(s)


def test_rank_movers_gainers_and_losers():
    quotes = {
        "HUM": SimpleNamespace(
            to_dict=lambda: {
                "last": 280.0,
                "session_change_pct": 14.5,
                "change_pct": 14.5,
                "session": "prepost",
            }
        ),
        "CCI": {
            "last": 110.0,
            "session_change_pct": 7.5,
            "change_pct": 7.5,
            "session": "prepost",
        },
        "TMUS": {
            "last": 220.0,
            "session_change_pct": -7.0,
            "change_pct": -7.0,
            "session": "prepost",
        },
        "PENNY": {
            "last": 0.5,
            "session_change_pct": 40.0,
            "change_pct": 40.0,
            "session": "prepost",
        },
    }
    gainers, losers = rank_movers(quotes, min_abs_pct=1.5, min_price=2.0, top_n=5)
    assert gainers[0]["symbol"] == "HUM"
    assert any(r["symbol"] == "CCI" for r in gainers)
    assert losers[0]["symbol"] == "TMUS"
    assert all(r["symbol"] != "PENNY" for r in gainers)


def test_fetch_board_merges_session_eye_with_mocks():
    quotes = {
        "HUM": {"last": 280.0, "session_change_pct": 14.0, "session": "prepost"},
        "MSFT": {"last": 420.0, "session_change_pct": 0.4, "session": "prepost"},
    }
    board = fetch_premarket_movers(
        {"actions": {"desk_special_eye": ["HOOD", "MSFT"], "premarket_min_abs_pct": 1.5}},
        quotes=quotes,
        top_n=8,
    )
    eye = set(board["session_eye"])
    assert "HUM" in eye and "TMUS" in eye and "HOOD" in eye
    assert board["counts"]["catalysts"] >= 12
    merged = effective_special_eye(
        {"actions": {"desk_special_eye": ["HOOD"]}},
        board=board,
    )
    assert {"HUM", "TMUS", "ALHC", "AAPL", "HOOD"} <= merged
