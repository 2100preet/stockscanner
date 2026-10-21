"""Tests for Telegram + multi-channel desk alerts (no live network)."""

from __future__ import annotations

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.alerts import telegram as tg


def test_telegram_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert tg.configured()["ok"] is False
    res = tg.send_telegram_text("hello")
    assert res["skipped"] is True


def test_telegram_send_posts(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")

    class FakeResp:
        status_code = 200
        content = b'{"ok":true,"result":{"message_id":42}}'

        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    calls = {}

    def fake_post(url, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return FakeResp()

    monkeypatch.setattr(tg.requests, "post", fake_post)
    res = tg.send_telegram_text("BUY AAPL")
    assert res["ok"] is True
    assert res["provider"] == "telegram"
    assert "123:ABC" in calls["url"]
    assert calls["json"]["chat_id"] == "987654321"
    assert "BUY AAPL" in calls["json"]["text"]
    assert "parse_mode" not in calls["json"]


def test_dispatch_sends_telegram(tmp_path, monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    sent: list[str] = []

    def fake_tg(body, **kwargs):
        sent.append(body)
        return {"ok": True, "provider": "telegram", "configured": True}

    monkeypatch.setattr(disp, "send_telegram_text", fake_tg)
    monkeypatch.setattr(
        disp,
        "whatsapp_configured",
        lambda: {"ok": False, "callmebot": False, "twilio": False, "meta": False},
    )

    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "MSFT",
                    "action": "BUY_NOW",
                    "contract": "MSFT261005C00500000",
                    "ask": 1.2,
                    "signaled_at": "2026-10-05T16:00:00+00:00",
                }
            ]
        },
        "lottery": {},
        "rip_radar": {},
        "beauty_monthly": {},
        "level_watch": {},
        "challenge": {},
        "odte_1k": {},
        "ml6": {},
    }
    seen = tmp_path / "seen.json"
    r1 = disp.dispatch_snapshot_alerts(snap, seen_path=seen)
    assert r1.get("primed") is True
    assert sent == []

    snap2 = {
        **snap,
        "actions": {
            "buy_now": [
                {
                    "symbol": "NVDA",
                    "action": "BUY_NOW",
                    "contract": "NVDA261005C00180000",
                    "ask": 0.8,
                    "signaled_at": "2026-10-05T17:00:00+00:00",
                }
            ]
        },
    }
    r3 = disp.dispatch_snapshot_alerts(snap2, seen_path=seen)
    assert r3.get("sent") == 1
    assert "telegram" in (r3.get("providers") or [])
    assert len(sent) == 1
    assert "NVDA" in sent[0]


def test_collects_gold_setup_and_golden_flow_only():
    snap = {
        "sure_winners": {
            "winners": [
                {
                    "symbol": "TSLA",
                    "action": "SURE_WINNER",
                    "sure_winner": True,
                    "confidence_tier": "HIGH",
                    "contract": "TSLA261023C00385000",
                    "strike": 385,
                    "expiry": "2026-10-23",
                    "ask": 7.6,
                    "signaled_at": "2026-10-20T20:54:37+00:00",
                },
                {
                    "symbol": "F",
                    "action": "SURE_WINNER",
                    "sure_winner": True,
                    "confidence_tier": "SOLID",
                    "contract": "F261023C00012000",
                    "ask": 0.4,
                },
            ]
        },
        "actions": {
            "buy_now": [
                {
                    "symbol": "JNJ",
                    "action": "SURE_WINNER",
                    "contract": "JNJ261023C00257500",
                    "ask": 1.1,
                }
            ]
        },
        "echo": {
            "option_flow": {
                "golden": [
                    {
                        "symbol": "LRCX",
                        "tier": "golden",
                        "flags": ["golden"],
                        "contract": "LRCX261023P00265000",
                        "right": "P",
                        "strike": 265,
                        "expiry": "2026-10-23",
                        "mid": 3.4,
                        "premium_notional": 250000,
                    },
                    {
                        "symbol": "LRCX",
                        "tier": "golden",
                        "contract": "LRCX261023P00265000",
                        "right": "P",
                        "strike": 265,
                        "expiry": "2026-10-23",
                        "mid": 3.4,
                    },
                ],
                "prints": [
                    {
                        "symbol": "AMD",
                        "tier": "unusual",
                        "flags": ["unusual"],
                        "contract": "AMD261023C00200000",
                        "right": "C",
                        "strike": 200,
                    },
                    {
                        "symbol": "XYZ",
                        "golden": True,
                        "pattern": "ascending triangle",
                        "contract": "",
                    },
                ],
            }
        },
        "chart_patterns": {
            "buy_pattern": [
                {"symbol": "TRI", "golden": True, "action": "BUY_PATTERN", "pattern": "golden triangle"}
            ]
        },
    }
    alerts = disp.collect_trade_alerts(snap)
    desks = [(a["desk"], a["symbol"]) for a in alerts]
    assert desks[0] == ("Gold Setup", "TSLA")
    assert ("Gold Setup", "F") not in desks
    assert ("Golden Flow", "LRCX") in desks
    assert sum(1 for desk, sym in desks if desk == "Golden Flow" and sym == "LRCX") == 1
    assert all(sym != "AMD" for _, sym in desks)
    assert all(sym != "XYZ" for _, sym in desks)
    assert all(sym != "TRI" for _, sym in desks)
    assert all(sym != "JNJ" for _, sym in desks)
    gold = alerts[0]["message"]
    assert gold.splitlines()[0] == "⭐ 🟢 SURE WINNER TSLA 385C 10/23 HIGH"
    assert "parse_mode" not in gold
    flow = next(a for a in alerts if a["desk"] == "Golden Flow")
    assert flow["ask"] == 3.4
    assert flow["message"].splitlines()[0] == "🔴 GOLDEN FLOW LRCX 265P 10/23"


def test_gold_and_golden_are_not_crowded_out(tmp_path, monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent: list[str] = []

    def fake_tg(body, **kwargs):
        sent.append(body)
        return {"ok": True, "provider": "telegram", "configured": True}

    monkeypatch.setattr(disp, "send_telegram_text", fake_tg)
    monkeypatch.setattr(
        disp,
        "whatsapp_configured",
        lambda: {"ok": False, "callmebot": False, "twilio": False, "meta": False},
    )
    seen = tmp_path / "seen.json"
    seed = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "MSFT",
                    "action": "BUY_NOW",
                    "contract": "MSFT1",
                    "signaled_at": "2026-10-20T15:00:00+00:00",
                }
            ]
        }
    }
    primed = disp.dispatch_snapshot_alerts(seed, seen_path=seen)
    assert primed.get("primed") is True

    rip = [
        {
            "symbol": f"R{i}",
            "action": "BUY_RIP",
            "contract": f"R{i}261023C00010000",
            "ask": 0.5,
            "signaled_at": f"2026-10-20T20:54:{i:02d}+00:00",
        }
        for i in range(10)
    ]
    snap = {
        "sure_winners": {
            "winners": [
                {
                    "symbol": "TSLA",
                    "action": "SURE_WINNER",
                    "sure_winner": True,
                    "confidence_tier": "MAX",
                    "contract": "TSLA261023C00385000",
                    "ask": 7.6,
                    "signaled_at": "2026-10-20T20:54:37+00:00",
                }
            ]
        },
        "echo": {
            "option_flow": {
                "prints": [
                    {
                        "symbol": "AMAT",
                        "tier": "golden",
                        "flags": ["golden"],
                        "contract": "AMAT261023C00420000",
                        "right": "C",
                        "strike": 420,
                        "expiry": "2026-10-23",
                        "mid": 2.2,
                    }
                ]
            }
        },
        "rip_radar": {"buy_rip": rip},
    }
    result = disp.dispatch_snapshot_alerts(snap, seen_path=seen)
    assert result.get("sent") == 8
    assert result.get("errors") in ([], None)
    assert any("SURE WINNER" in body and "TSLA" in body for body in sent)
    assert any("GOLDEN FLOW" in body and "AMAT" in body for body in sent)
    assert sent[0].splitlines()[0] == "⭐ 🟢 SURE WINNER TSLA 385C 10/23 MAX"
    assert sent[1].splitlines()[0] == "🟢 GOLDEN FLOW AMAT 420C 10/23"


def test_signal_headline_uses_sentiment_when_right_is_missing():
    bear = disp._fmt_alert(
        {"symbol": "SPY", "sentiment": "bearish", "strike": 500, "expiry": "2026-10-16", "ask": 2.0},
        "BUY",
        "Golden Flow",
    )
    bull = disp._fmt_alert(
        {
            "symbol": "QQQ",
            "sentiment": "bullish",
            "right": "C",
            "strike": 480.0,
            "expiry": "2026-10-16",
            "confidence_tier": "MAX",
            "ask": 1.5,
        },
        "BUY",
        "Gold Setup",
    )
    assert bear.splitlines()[0] == "🔴 GOLDEN FLOW SPY 500 10/16"
    assert bull.splitlines()[0] == "⭐ 🟢 SURE WINNER QQQ 480C 10/16 MAX"
    assert "parse_mode" not in bear
    assert "parse_mode" not in bull
