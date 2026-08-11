"""Tests for persistent recommendation logger."""
from __future__ import annotations

from pathlib import Path

from odte_scanner.trading.rec_log import RecommendationLog


def test_lottery_entry_exit_pnl(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_lottery(
        {
            "buy_now": [
                {
                    "symbol": "NVDA",
                    "ask": 1.20,
                    "contract": "NVDA250807C00120000",
                    "expiry": "2025-08-07",
                    "strike": 120,
                    "dte": 0,
                    "headline": "BUY NOW NVDA",
                    "detail": "convex + tape",
                }
            ],
            "sell_now": [],
        }
    )
    board = log.board(section="lottery")
    assert board["open"] == 1
    assert board["open_recs"][0]["symbol"] == "NVDA"
    assert board["open_recs"][0]["entry_price"] == 1.2

    log.sync_lottery(
        {
            "buy_now": [],
            "sell_now": [
                {
                    "symbol": "NVDA",
                    "bid": 2.40,
                    "headline": "SELL NOW",
                    "detail": "bank +100%",
                }
            ],
        }
    )
    board = log.board(section="lottery")
    assert board["open"] == 0
    assert board["closed"] == 1
    closed = board["closed_recs"][0]
    assert closed["profit_pct"] == 100.0
    assert closed["pnl_usd"] == 120.0  # (2.4-1.2)*100
    assert closed["close_action"] == "SELL_NOW"


def test_challenge_entry_persists_when_off_board(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_challenge(
        {
            "tickets": [
                {
                    "symbol": "MU",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 3.5,
                    "horizon": "swing",
                    "reasons": ["hist 100%", "post-earn"],
                }
            ],
            "entry": [],
            "exit": [],
        }
    )
    assert log.board(section="challenge")["open"] == 1
    assert log.board(section="challenge")["open_recs"][0]["on_board"] is True

    # Next day: MU gone from board — history remains, flagged off-board
    log.sync_challenge({"tickets": [], "entry": [], "exit": []})
    board = log.board(section="challenge")
    assert board["open"] == 1
    assert board["open_recs"][0]["symbol"] == "MU"
    assert board["open_recs"][0]["on_board"] is False


def test_challenge_wait_not_opened_as_entry(tmp_path: Path):
    """WAIT/HOLD must not open ENTRY recs — that starved EXIT/P&L history."""
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_challenge(
        {
            "primary": {"symbol": "TOST", "right": "C", "action": "WAIT", "ask": None},
            "tickets": [
                {"symbol": "TOST", "right": "C", "action": "WAIT"},
                {"symbol": "AMZN", "right": "C", "action": "WAIT", "ask": 2.1},
                {
                    "symbol": "MU",
                    "right": "C",
                    "action": "ENTRY",
                    "ask": 3.5,
                    "reasons": ["hist"],
                },
            ],
        }
    )
    board = log.board(section="challenge")
    assert board["open"] == 1
    assert board["open_recs"][0]["symbol"] == "MU"
    assert board["open_recs"][0]["open_action"] == "ENTRY"


def test_challenge_exit_closes_with_pnl(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.note_entry(
        section="challenge",
        symbol="APP",
        action="ENTRY",
        right="C",
        price=2.0,
        reason="hist win",
    )
    log.note_exit(
        section="challenge",
        symbol="APP",
        action="EXIT",
        right="C",
        price=1.0,
        reason="stop",
    )
    closed = log.board(section="challenge")["closed_recs"][0]
    assert closed["profit_pct"] == -50.0
    assert closed["pnl_usd"] == -100.0


def test_actions_sections(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_actions(
        {
            "buy_now": [
                {"symbol": "SPY", "ask": 0.55, "dte_bucket": "0dte", "contract": "SPY_SYN"},
                {"symbol": "AAPL", "ask": 1.1, "dte_bucket": "weekly"},
            ],
            "sell_now": [],
        }
    )
    assert log.board(section="odte")["open"] == 1
    assert log.board(section="weekly")["open"] == 1
