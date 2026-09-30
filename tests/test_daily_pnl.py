"""Tests for unified daily P&L dashboard aggregator."""

from __future__ import annotations

from odte_scanner.trading.daily_pnl import build_daily_pnl


def test_daily_pnl_rolls_up_closed_by_exit_day():
    journal_book = {
        "trades": [
            {
                "id": "j1",
                "symbol": "NVDA",
                "right": "C",
                "contract": "NVDA260930C00100000",
                "dte_bucket": "0dte",
                "status": "closed",
                "entered_at": "2026-09-29T14:00:00+00:00",
                "exited_at": "2026-09-29T18:00:00+00:00",
                "entry_ask": 1.0,
                "exit_bid": 1.5,
                "pnl_usd": 50.0,
                "profit_pct": 50.0,
                "contracts": 1,
                "entry_reason": "BUY NOW 0DTE",
                "exit_reason": "SELL NOW",
            },
            {
                "id": "j2",
                "symbol": "AMD",
                "right": "C",
                "dte_bucket": "weekly",
                "status": "open",
                "entered_at": "2026-09-29T15:00:00+00:00",
                "entry_ask": 2.0,
                "mark": 2.2,
                "unrealized_pnl_usd": 20.0,
                "unrealized_pct": 10.0,
                "contracts": 1,
                "entry_reason": "BUY NOW weekly",
            },
        ]
    }
    challenge = {
        "book": {
            "trades": [
                {
                    "id": "c1",
                    "symbol": "JPM",
                    "right": "C",
                    "horizon": "sprint",
                    "status": "closed",
                    "entered_at": "2026-09-28T15:00:00+00:00",
                    "exited_at": "2026-09-29T16:00:00+00:00",
                    "entry_ask": 0.5,
                    "exit_bid": 0.8,
                    "pnl_usd": 30.0,
                    "profit_pct": 60.0,
                    "contracts": 1,
                    "entry_reason": "ENTRY challenge",
                }
            ]
        }
    }
    odte_1k = {
        "book": {
            "trades": [
                {
                    "id": "o1",
                    "symbol": "SPY",
                    "right": "P",
                    "status": "closed",
                    "entered_at": "2026-09-29T14:35:00+00:00",
                    "exited_at": "2026-09-29T19:00:00+00:00",
                    "entry_ask": 1.2,
                    "exit_bid": 0.9,
                    "pnl_usd": -30.0,
                    "profit_pct": -25.0,
                    "contracts": 1,
                    "entry_reason": "PUT NOW ORB15",
                }
            ]
        }
    }
    board = build_daily_pnl(
        journal_book=journal_book,
        challenge=challenge,
        odte_1k=odte_1k,
        rec_log={"closed_recs": [], "open_recs": []},
    )
    assert board["totals"]["closed_n"] == 3
    assert board["totals"]["open_n"] == 1
    assert board["totals"]["realized_pnl_usd"] == 50.0  # 50+30-30
    assert board["totals"]["win_n"] == 2
    assert board["totals"]["loss_n"] == 1
    assert any(d["day"] == "2026-09-29" for d in board["by_day"])
    cats = {c["category"] for c in board["by_category"]}
    assert "0dte" in cats
    assert "challenge" in cats
    assert "odte_1k" in cats
    closed = board["closed"]
    assert all("entered_at" in r and "category" in r for r in closed)
    assert any(r["symbol"] == "NVDA" and r["entry_ask"] == 1.0 and r["exit_bid"] == 1.5 for r in closed)


def test_daily_pnl_infers_ml6_and_lottery():
    journal_book = {
        "trades": [
            {
                "id": "m1",
                "symbol": "NBIS",
                "right": "C",
                "status": "closed",
                "dte_bucket": "weekly",
                "entered_at": "2026-09-29T14:00:00+00:00",
                "exited_at": "2026-09-29T20:00:00+00:00",
                "entry_ask": 1.0,
                "exit_bid": 1.1,
                "pnl_usd": 10.0,
                "entry_reason": "BUY NOW ML6 neocloud print",
            },
            {
                "id": "l1",
                "symbol": "SMCI",
                "right": "C",
                "status": "closed",
                "dte_bucket": "lottery",
                "entered_at": "2026-09-29T14:00:00+00:00",
                "exited_at": "2026-09-29T20:00:00+00:00",
                "entry_ask": 0.4,
                "exit_bid": 0.9,
                "pnl_usd": 50.0,
                "entry_reason": "Explosive lottery",
            },
        ]
    }
    board = build_daily_pnl(journal_book=journal_book)
    cats = {r["category"] for r in board["closed"]}
    assert "ml6" in cats
    assert "lottery" in cats


def test_recommended_not_taken_excluded_when_already_took():
    journal_book = {
        "trades": [
            {
                "id": "j1",
                "symbol": "AMAT",
                "right": "C",
                "dte_bucket": "weekly",
                "status": "closed",
                "entered_at": "2026-09-29T14:00:00+00:00",
                "exited_at": "2026-09-29T18:00:00+00:00",
                "entry_ask": 10.0,
                "exit_bid": 12.0,
                "pnl_usd": 200.0,
            }
        ]
    }
    rec_log = {
        "closed_recs": [
            {
                "section": "weekly",
                "symbol": "AMAT",
                "right": "C",
                "status": "closed",
                "source": "actions",
                "recommended_at": "2026-09-29T14:00:00+00:00",
                "closed_at": "2026-09-29T18:00:00+00:00",
                "entry_price": 10.0,
                "exit_price": 12.0,
                "pnl_usd": 200.0,
            },
            {
                "section": "radar",
                "symbol": "QQQ",
                "right": "C",
                "status": "closed",
                "source": "radar",
                "recommended_at": "2026-09-29T14:30:00+00:00",
                "closed_at": "2026-09-29T19:00:00+00:00",
                "entry_price": 2.0,
                "exit_price": 1.5,
                "pnl_usd": -50.0,
            },
        ]
    }
    board = build_daily_pnl(journal_book=journal_book, rec_log=rec_log)
    # AMAT already took — not in recommended_not_taken; QQQ radar is
    assert board["totals"]["closed_n"] == 1
    assert board["totals"]["recommended_not_taken_n"] == 1
    assert board["recommended_not_taken"][0]["symbol"] == "QQQ"
    assert board["recommended_not_taken"][0]["took"] is False
