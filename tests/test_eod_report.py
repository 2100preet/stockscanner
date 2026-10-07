"""EOD Telegram report at 3:00 PM ET."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from odte_scanner.alerts import eod_report as eod

ET = ZoneInfo("America/New_York")


def test_in_eod_window_at_3pm_et():
    # 2026-10-07 is a Wednesday
    at = datetime(2026, 10, 7, 15, 5, tzinfo=ET)
    assert eod.in_eod_window(at) is True
    before = datetime(2026, 10, 7, 14, 59, tzinfo=ET)
    assert eod.in_eod_window(before) is False
    after = datetime(2026, 10, 7, 15, 25, tzinfo=ET)
    assert eod.in_eod_window(after) is False
    weekend = datetime(2026, 10, 10, 15, 5, tzinfo=ET)  # Saturday
    assert eod.in_eod_window(weekend) is False


def test_build_eod_report_message():
    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "NVDA",
                    "right": "C",
                    "strike": 180,
                    "ask": 1.25,
                    "action": "BUY_NOW",
                    "signaled_at": "2026-10-07T14:00:00+00:00",
                }
            ],
            "sell_now": [
                {
                    "symbol": "AAPL",
                    "right": "C",
                    "strike": 250,
                    "entry_ask": 2.0,
                    "bid": 2.8,
                    "ask": 2.8,
                    "action": "SELL_NOW",
                    "signaled_at": "2026-10-07T18:00:00+00:00",
                }
            ],
        },
        "call_candidates": [
            {"symbol": "GLD", "strike": 375, "right": "P", "bid": 1.29, "ask": 1.35},
        ],
        "rec_log": {
            "closed_pnl_usd": -130.0,
            "closed_recs": [
                {
                    "symbol": "GLD",
                    "right": "P",
                    "strike": 375,
                    "open_action": "WAIT",
                    "status": "lapsed",
                    "section": "odte",
                    "entry_price": 0.14,
                    "recommended_at": "2026-10-07T16:28:00+00:00",
                    "headline": "WAIT GLD PUT",
                }
            ],
            "open_recs": [],
        },
        "daily_pnl": {
            "closed": [
                {
                    "symbol": "MSFT",
                    "right": "C",
                    "strike": 400,
                    "status": "closed",
                    "took": True,
                    "day_exit": "2026-10-07",
                    "entry_ask": 1.0,
                    "exit_bid": 1.5,
                    "pnl_usd": 50.0,
                    "profit_pct": 50.0,
                    "exited_at": "2026-10-07T19:00:00+00:00",
                    "trade_name": "MSFT 400C",
                }
            ],
            "open": [
                {
                    "symbol": "TSLA",
                    "right": "C",
                    "strike": 250,
                    "status": "open",
                    "took": True,
                    "day_entry": "2026-10-07",
                    "entry_ask": 3.0,
                    "mark": 3.4,
                    "unrealized_pnl_usd": 40.0,
                    "entered_at": "2026-10-07T15:30:00+00:00",
                    "trade_name": "TSLA 250C",
                }
            ],
            "by_day": [{"day": "2026-10-07", "realized_pnl_usd": 50.0}],
            "recommended_not_taken": [],
        },
    }
    now = datetime(2026, 10, 7, 15, 2, tzinfo=ET)
    report = eod.build_eod_report(snap, now=now)
    assert report["closed_pnl_usd"] == 50.0
    assert report["open_pnl_usd"] == 40.0
    assert report["missed"]
    assert report["missed"][0]["symbol"] == "GLD"
    assert report["missed"][0]["profit_pct"] >= 800
    msg = report["message"]
    assert "EOD desk report" in msg
    assert "MISSED" in msg
    assert "GLD" in msg
    assert "WAIT" in msg
    assert "NVDA" in msg
    assert "AAPL" in msg
    assert "bought $2.00 → sell $2.80" in msg
    assert "Paper closed P&L" in msg
    assert "MSFT" in msg
    assert "TSLA" in msg


def test_maybe_send_once_per_day(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    monkeypatch.delenv("EOD_TELEGRAM_ENABLED", raising=False)

    sent: list[str] = []

    def fake_send(body, **_k):
        sent.append(body)
        return {"ok": True, "message_id": 7, "provider": "telegram"}

    monkeypatch.setattr(eod, "send_telegram_text", fake_send)

    snap = {
        "actions": {"buy_now": [], "sell_now": []},
        "daily_pnl": {"closed": [], "open": [], "by_day": []},
    }
    state = tmp_path / "eod.json"
    now = datetime(2026, 10, 7, 15, 3, tzinfo=ET)

    r1 = eod.maybe_send_eod_report(snap, now=now, state_path=state)
    assert r1.get("sent") is True
    assert len(sent) == 1

    r2 = eod.maybe_send_eod_report(snap, now=now, state_path=state)
    assert r2.get("skipped") is True
    assert r2.get("reason") == "already_sent"
    assert len(sent) == 1


def test_fmt_sell_alert_shows_bought_to_sell():
    from odte_scanner.alerts.dispatcher import _fmt_alert

    msg = _fmt_alert(
        {
            "symbol": "NVDA",
            "right": "C",
            "strike": 180,
            "entry_ask": 1.1,
            "bid": 1.65,
            "ask": 1.65,
            "signaled_at_cst": "Oct 7, 2026, 2:10:00 PM CDT",
            "entered_at_cst": "Oct 7, 2026, 10:05:00 AM CDT",
            "detail": "take profit",
        },
        "SELL",
        "Options",
    )
    assert "BUY $1.10 → SELL $1.65" in msg
    assert "Bought at" in msg
    assert "Asked to sell" in msg
