"""Tests for WhatsApp alert dispatch (no live network)."""

from __future__ import annotations

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.alerts import whatsapp as wa


def test_whatsapp_not_configured(monkeypatch):
    for k in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_WHATSAPP_FROM",
        "WHATSAPP_TO",
        "WHATSAPP_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = wa.configured()
    assert cfg["ok"] is False
    res = wa.send_whatsapp_text("hello")
    assert res["skipped"] is True


def test_normalize_to():
    assert wa._normalize_to("+15551234567") == "whatsapp:+15551234567"
    assert wa._normalize_to("whatsapp:+15551234567") == "whatsapp:+15551234567"
    assert wa._normalize_to("15551234567") == "whatsapp:+15551234567"


def test_collect_trade_alerts_from_snapshot():
    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "AAPL",
                    "action": "BUY_NOW",
                    "strike": 335,
                    "expiry": "2026-10-05",
                    "ask": 0.55,
                    "right": "C",
                    "signaled_at_cst": "Oct 5, 2026, 11:55:07 AM CDT",
                }
            ],
            "sell_now": [],
        },
        "rip_radar": {
            "buy_rip": [
                {
                    "symbol": "TSLA",
                    "action": "BUY_RIP",
                    "strike": 390,
                    "expiry": "2026-10-09",
                    "ask": 2.7,
                    "contract": "TSLA261009C00390000",
                }
            ]
        },
        "lottery": {"buy_now": [], "sell_now": []},
        "beauty_monthly": {},
        "level_watch": {},
        "challenge": {},
        "odte_1k": {},
        "ml6": {},
    }
    alerts = disp.collect_trade_alerts(snap)
    assert len(alerts) == 2
    symbols = {a["symbol"] for a in alerts}
    assert symbols == {"AAPL", "TSLA"}
    assert all("BUY" in a["side"] for a in alerts)
    assert "AAPL" in alerts[0]["message"] or "AAPL" in alerts[1]["message"]


def test_dispatch_primes_then_sends(tmp_path, monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    monkeypatch.delenv("CALLMEBOT_API_KEY", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    monkeypatch.setenv("WHATSAPP_TO", "+15551234567")

    sent: list[str] = []

    def fake_send(body, **kwargs):
        sent.append(body)
        return {"ok": True, "provider": "twilio", "configured": True}

    monkeypatch.setattr(disp, "send_whatsapp_text", fake_send)

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
    assert r1.get("sent") == 0
    assert sent == []

    # Same pulse — still no send
    r2 = disp.dispatch_snapshot_alerts(snap, seen_path=seen)
    assert r2.get("sent") == 0

    # New pulse
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
    assert len(sent) == 1
    assert "NVDA" in sent[0]


def test_callmebot_send(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TO", "15551234567")
    monkeypatch.setenv("CALLMEBOT_APIKEY", "123456")
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"):
        monkeypatch.delenv(k, raising=False)

    class FakeResp:
        status_code = 200
        text = "OK"

    calls = {}

    def fake_get(url, timeout=None):
        calls["url"] = url
        return FakeResp()

    monkeypatch.setattr(wa.requests, "get", fake_get)
    res = wa.send_whatsapp_text("BUY TSLA")
    assert res["ok"] is True
    assert res["provider"] == "callmebot"
    assert "15551234567" in calls["url"]
    assert "apikey=123456" in calls["url"]


def test_twilio_send_posts(monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    monkeypatch.delenv("CALLMEBOT_API_KEY", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    monkeypatch.setenv("WHATSAPP_TO", "+15551234567")

    class FakeResp:
        status_code = 201
        content = b'{"sid":"SM123","status":"queued"}'

        def json(self):
            return {"sid": "SM123", "status": "queued"}

    calls = {}

    def fake_post(url, data=None, auth=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        calls["auth"] = auth
        return FakeResp()

    monkeypatch.setattr(wa.requests, "post", fake_post)
    res = wa.send_whatsapp_text("BUY AAPL")
    assert res["ok"] is True
    assert res["provider"] == "twilio"
    assert calls["data"]["To"] == "whatsapp:+15551234567"
    assert "BUY AAPL" in calls["data"]["Body"]
