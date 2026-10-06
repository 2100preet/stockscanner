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
