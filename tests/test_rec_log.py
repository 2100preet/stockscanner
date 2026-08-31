"""Tests for persistent recommendation logger."""
from __future__ import annotations

import json
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
    assert board["wins"] == 1
    assert board["losses"] == 0
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


def test_challenge_wait_logged_as_wait_not_entry(tmp_path: Path):
    """WAIT opens soft WAIT recs; only ENTRY opens as ENTRY."""
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
    assert board["open"] >= 2
    by_sym = {r["symbol"]: r for r in board["open_recs"]}
    assert by_sym["MU"]["open_action"] == "ENTRY"
    assert by_sym["TOST"]["open_action"] == "WAIT"
    assert by_sym["AMZN"]["entry_price"] == 2.1


def test_lottery_wait_logged_with_ask(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_lottery(
        {
            "buy_now": [],
            "sell_now": [],
            "wait": [
                {
                    "symbol": "SPY",
                    "ask": 1.36,
                    "bid": 1.35,
                    "strike": 776,
                    "contract": "SPY260813C00776000",
                    "expiry": "2026-08-13",
                    "dte": 0,
                    "headline": "WAIT LOTTERY SPY",
                    "detail": "no tape yet",
                },
                {
                    "symbol": "GOOGL",
                    "ask": 2.47,
                    "contract": "GOOGL260814P00345000",
                    "strike": 345,
                    "headline": "WAIT LOTTERY GOOGL",
                },
            ],
        }
    )
    board = log.board(section="lottery")
    assert board["open"] == 2
    spy = next(r for r in board["open_recs"] if r["symbol"] == "SPY")
    assert spy["open_action"] == "WAIT"
    assert spy["entry_price"] == 1.36
    googl = next(r for r in board["open_recs"] if r["symbol"] == "GOOGL")
    assert googl["right"] == "P"


def test_wait_upgrades_to_buy_then_sell_pnl(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_lottery({"buy_now": [], "sell_now": [], "wait": [{"symbol": "NVDA", "ask": 1.0, "contract": "NVDA_C", "strike": 120}]})
    log.sync_lottery(
        {
            "buy_now": [{"symbol": "NVDA", "ask": 1.2, "contract": "NVDA_C", "strike": 120, "headline": "BUY NOW"}],
            "sell_now": [],
            "wait": [],
        }
    )
    open_rec = log.board(section="lottery")["open_recs"][0]
    assert open_rec["open_action"] == "BUY_NOW"
    assert open_rec["entry_price"] == 1.2  # locked at BUY ask, not WAIT 1.0
    log.sync_lottery({"buy_now": [], "sell_now": [{"symbol": "NVDA", "bid": 2.4, "detail": "bank"}], "wait": []})
    board = log.board(section="lottery")
    assert board["wins"] == 1
    assert board["closed_pnl_usd"] == 120.0


def test_actions_wait_fills_odte_weekly_sections(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_actions(
        {
            "buy_now": [],
            "sell_now": [],
            "wait": [
                {"symbol": "SPY", "ask": 1.36, "dte_bucket": "0dte", "contract": "SPY_C", "strike": 776, "headline": "WAIT SPY"},
                {"symbol": "AAPL", "ask": 3.0, "dte_bucket": "weekly", "contract": "AAPL_P", "strike": 305, "right": "P", "headline": "WAIT AAPL"},
            ],
        }
    )
    assert log.board(section="odte")["open"] == 1
    assert log.board(section="weekly")["open"] == 1
    assert log.board(section="weekly")["open_recs"][0]["right"] == "P"


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
    board = log.board(section="challenge")
    closed = board["closed_recs"][0]
    assert closed["profit_pct"] == -50.0
    assert closed["pnl_usd"] == -100.0
    assert board["wins"] == 0
    assert board["losses"] == 1
    assert board["closed_pnl_usd"] == -100.0


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


def test_clock_flatten_does_not_count_zero_as_loss(tmp_path: Path):
    """Off-board time-stop must lapse — never invent exit=entry $0 losses."""
    from unittest.mock import patch

    log = RecommendationLog(tmp_path / "rec.json")
    log.note_entry(
        section="odte",
        symbol="SMCI",
        action="BUY_NOW",
        price=1.5,
        dte=0,
        horizon="0dte",
        contract="SMCI_OPT",
        strike=40,
    )
    with patch(
        "odte_scanner.signals.hold_rules.time_stop_reason",
        return_value="0DTE time-stop — flatten by 15:45 ET",
    ):
        log.mark_off_board("odte", live_keys=set())
    board = log.board(section="odte")
    assert board["open"] == 0
    assert board["losses"] == 0
    assert board["wins"] == 0
    assert board["closed_pnl_usd"] == 0
    assert board["lapsed"] == 1
    assert board["closed_recs"][0]["status"] == "lapsed"
    assert board["closed_recs"][0]["pnl_usd"] is None


def test_scrub_legacy_zero_clock_closes(tmp_path: Path):
    path = tmp_path / "rec.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": "x",
                "recommendations": [
                    {
                        "id": "rec-1",
                        "section": "odte",
                        "symbol": "CBRS",
                        "right": "C",
                        "open_action": "BUY_NOW",
                        "recommended_at": "2026-08-11T10:00:00+00:00",
                        "last_recommended_at": "2026-08-11T20:00:00+00:00",
                        "entry_price": 264.5,
                        "status": "closed",
                        "close_action": "EXIT",
                        "closed_at": "2026-08-11T20:45:00+00:00",
                        "exit_price": 264.5,
                        "exit_reason": "0DTE time-stop — flatten by 15:45 ET",
                        "profit_pct": 0.0,
                        "pnl_usd": 0.0,
                        "events": [],
                    }
                ],
            }
        )
    )
    log = RecommendationLog(path)
    board = log.board()
    assert board["losses"] == 0
    assert board["lapsed"] == 1
    assert board["closed_pnl_usd"] == 0


def test_quality_cards_do_not_open_stock_last_as_buy(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    n = log.sync_action_cards(
        {
            "0dte_quality": [
                {"symbol": "CBRS", "last_price": 264.5, "entry": 264.5, "reasons": ["gap"]}
            ]
        }
    )
    assert n == 0
    assert log.board(section="odte")["open"] == 0


def test_journal_buy_sell_drives_priced_pnl(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_from_journal(
        {
            "trades": [
                {
                    "id": "j1",
                    "symbol": "SPY",
                    "right": "C",
                    "contract": "SPY260812C00770000",
                    "dte_bucket": "0dte",
                    "status": "open",
                    "entry_ask": 2.0,
                    "entered_at": "2026-08-12T14:00:00+00:00",
                    "entry_reason": "BUY NOW",
                    "strike": 770,
                    "expiry": "2026-08-12",
                }
            ]
        }
    )
    assert log.board(section="odte")["open"] == 1
    assert log.board(section="odte")["open_recs"][0]["entry_price"] == 2.0

    log.sync_from_journal(
        {
            "trades": [
                {
                    "id": "j1",
                    "symbol": "SPY",
                    "right": "C",
                    "contract": "SPY260812C00770000",
                    "dte_bucket": "0dte",
                    "status": "closed",
                    "entry_ask": 2.0,
                    "exit_bid": 3.9,
                    "entered_at": "2026-08-12T14:00:00+00:00",
                    "exited_at": "2026-08-12T18:00:00+00:00",
                    "exit_reason": "take profit +95%",
                    "strike": 770,
                    "expiry": "2026-08-12",
                }
            ]
        }
    )
    board = log.board(section="odte")
    assert board["open"] == 0
    assert board["closed"] == 1
    assert board["wins"] == 1
    assert board["closed_pnl_usd"] == 190.0  # (3.9-2.0)*100
    closed = board["closed_recs"][0]
    assert closed["entry_price"] == 2.0
    assert closed["exit_price"] == 3.9
    assert closed["profit_pct"] == 95.0


def test_sell_only_stub_not_created_without_entry(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    out = log.note_exit(
        section="odte",
        symbol="SPY",
        action="SELL_NOW",
        price=3.9,
        reason="take profit",
    )
    assert out is None
    assert log.board(section="odte")["closed"] == 0


def test_zero_pct_is_scratch_not_loss(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.note_entry(section="odte", symbol="QQQ", action="BUY_NOW", price=1.0)
    log.note_exit(section="odte", symbol="QQQ", action="SELL_NOW", price=1.0, reason="flat")
    board = log.board(section="odte")
    assert board["wins"] == 0
    assert board["losses"] == 0
    assert board["scratches"] == 1
    assert board["closed"] == 1


def test_radar_soft_no_clock_loss(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.sync_radar(
        {
            "hot": [],
            "watch": [
                {
                    "symbol": "IWM",
                    "ask": 0.86,
                    "action": "RADAR_WATCH",
                    "contract": "IWM_OPT",
                    "strike": 300,
                    "dte": 0,
                }
            ],
        }
    )
    open_rec = log.book.recommendations[0]
    open_rec.recommended_at = "2020-01-01T15:00:00+00:00"
    log.sync_radar({"hot": [], "watch": []})
    board = log.board(section="radar")
    # Still open / off-board — not a $0 loss
    assert board["losses"] == 0
    assert board["lapsed"] == 0
    assert board["open"] == 1
    assert board["open_recs"][0]["on_board"] is False


def test_stale_board_clock_exit_becomes_lapse_not_huge_loss(tmp_path: Path):
    """Weekly ALAB-style bug: clock flatten with stale $1.50 bid must not count as -$1000."""
    log = RecommendationLog(tmp_path / "rec.json")
    log.note_entry(
        section="weekly",
        symbol="ALAB",
        action="BUY_NOW",
        right="P",
        price=11.35,
        contract="ALAB260828P00270000",
        expiry="2026-08-28",
        strike=270,
        source="board",
    )
    out = log.note_exit(
        section="weekly",
        symbol="ALAB",
        action="SELL_NOW",
        right="P",
        price=1.5,
        reason="time-stop — flatten lottery by 15:45 ET (no gamma into the close)",
    )
    assert out is not None
    assert out.status == "lapsed"
    assert out.pnl_usd is None
    board = log.board(section="weekly")
    assert board["closed_pnl_usd"] == 0
    assert board["losses"] == 0


def test_entry_price_locked_after_first_buy_now(tmp_path: Path):
    log = RecommendationLog(tmp_path / "rec.json")
    log.note_entry(section="odte", symbol="SPY", action="BUY_NOW", price=2.0, source="board")
    log.note_entry(section="odte", symbol="SPY", action="BUY_NOW", price=2.8, source="board")
    open_rec = log._open_for("odte", "SPY", "C")
    assert open_rec is not None
    assert open_rec.entry_price == 2.0


def test_scrub_stale_board_exits_on_load(tmp_path: Path):
    path = tmp_path / "rec.json"
    raw = {
        "updated_at": "2026-08-31T00:00:00+00:00",
        "recommendations": [
            {
                "id": "rec-bad1",
                "section": "weekly",
                "symbol": "ALAB",
                "right": "P",
                "open_action": "BUY_NOW",
                "recommended_at": "2026-08-24T14:17:18+00:00",
                "last_recommended_at": "2026-08-24T20:51:52+00:00",
                "entry_price": 13.3,
                "status": "closed",
                "on_board": True,
                "close_action": "SELL_NOW",
                "closed_at": "2026-08-24T20:51:52+00:00",
                "exit_price": 1.5,
                "exit_reason": "time-stop — flatten lottery by 15:45 ET",
                "profit_pct": -88.72,
                "pnl_usd": -1180.0,
                "source": "board",
                "events": [],
            }
        ],
    }
    path.write_text(json.dumps(raw))
    log = RecommendationLog(path)
    rec = log.book.recommendations[0]
    assert rec.status == "lapsed"
    assert rec.pnl_usd is None
    assert log.board()["closed_pnl_usd"] == 0

