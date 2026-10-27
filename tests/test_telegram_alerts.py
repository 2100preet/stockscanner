"""Tests for Telegram + multi-channel desk alerts (no live network)."""

from __future__ import annotations

from odte_scanner.alerts import dispatcher as disp
from odte_scanner.alerts import telegram as tg


def test_missing_channel_does_not_seed_seen(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT", raising=False)
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    monkeypatch.delenv("CALLMEBOT_API_KEY", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_CLOUD_TOKEN", raising=False)
    monkeypatch.setattr(
        disp,
        "telegram_configured",
        lambda: {"ok": False, "token_set": False, "chat_set": False, "enabled": True},
    )
    monkeypatch.setattr(
        disp,
        "whatsapp_configured",
        lambda: {"ok": False, "callmebot": False, "twilio": False, "meta": False},
    )
    snap = {
        "actions": {
            "buy_now": [
                {
                    "symbol": "AMAT",
                    "action": "BUY_NOW",
                    "contract": "AMAT261120C00420000",
                    "ask": 4.2,
                    "signaled_at": "2026-10-22T13:35:00+00:00",
                }
            ]
        }
    }
    seen = tmp_path / "seen.json"
    res = disp.dispatch_snapshot_alerts(snap, seen_path=seen)
    assert res.get("primed") is False
    assert res.get("sent") == 0
    assert res.get("configured") is False
    assert "No alert channel" in str(res.get("error") or "")
    assert not seen.exists()


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
    assert r3["attempts"][0]["symbol"] == "NVDA"
    assert r3["attempts"][0]["contract"] == "NVDA261005C00180000"
    assert r3["attempts"][0]["kind"] == "Options"
    assert r3["attempts"][0]["ok"] is True


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
    snap["echo"]["option_flow"]["golden"].append(
        {
            "symbol": "NVDA",
            "tier": "golden",
            "flags": ["golden"],
            "contract": "NVDA261023C00180000",
            "right": "C",
            "strike": 180,
            "expiry": "2026-10-23",
            "mid": 2.4,
            "premium_notional": 250000,
        }
    )
    alerts = disp.collect_trade_alerts(snap)
    desks = [(a["desk"], a["symbol"]) for a in alerts]
    assert desks[0] == ("Gold Setup", "TSLA")
    assert ("Gold Setup", "F") not in desks
    # Puts stay on the card with a reason and are not sent.
    assert ("Golden Flow", "LRCX") not in desks
    assert ("Golden Flow", "NVDA") in desks
    assert all(sym != "AMD" for _, sym in desks)
    assert all(sym != "XYZ" for _, sym in desks)
    assert all(sym != "TRI" for _, sym in desks)
    assert all(sym != "JNJ" for _, sym in desks)
    gold = alerts[0]["message"]
    assert gold.splitlines()[0] == "⭐ 🟢 SURE WINNER TSLA 385C 10/23 HIGH"
    assert "parse_mode" not in gold
    flow = next(a for a in alerts if a["desk"] == "Golden Flow")
    assert flow["ask"] == 2.4
    assert flow["message"].splitlines()[0] == "🟢 GOLDEN FLOW NVDA 180C 10/23"
    put_line = disp._fmt_alert(snap["echo"]["option_flow"]["golden"][0], "BUY", "Golden Flow")
    assert put_line.splitlines()[0] == "🔴 GOLDEN FLOW LRCX 265P 10/23"


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
    from_occ = disp._fmt_alert(
        {
            "symbol": "TSLA",
            "action": "SURE_WINNER",
            "confidence_tier": "HIGH",
            "contract": "TSLA261023P00385000",
            "ask": 7.6,
        },
        "BUY",
        "Gold Setup",
    )
    assert from_occ.splitlines()[0] == "⭐ 🔴 SURE WINNER TSLA 385P 10/23 HIGH"
    assert "parse_mode" not in bear
    assert "parse_mode" not in bull


def _flow(sym, strike, expiry, *, right="C", premium=100_000, flow_score=10, ask=1.2):
    yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    occ = f"{sym}{yymmdd}{right}{int(round(float(strike) * 1000)):08d}"
    return {
        "symbol": sym,
        "tier": "golden",
        "flags": ["golden"],
        "contract": occ,
        "right": right,
        "strike": strike,
        "expiry": expiry,
        "ask": ask,
        "premium_notional": premium,
        "flow_score": flow_score,
        "action": "GOLDEN_FLOW",
    }


def _prime(tmp_path, monkeypatch):
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
    primed = disp.dispatch_snapshot_alerts(
        {"actions": {"buy_now": [{"symbol": "MSFT", "action": "BUY_NOW", "contract": "MSFT1", "ask": 1.0}]}},
        seen_path=seen,
    )
    assert primed.get("primed") is True
    return seen, sent


def test_golden_flow_and_gold_setup_collapse_to_one_message_per_symbol():
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
                    "right": "C",
                    "ask": 7.6,
                    "score": 90,
                },
                {
                    "symbol": "TSLA",
                    "action": "SURE_WINNER",
                    "sure_winner": True,
                    "confidence_tier": "MAX",
                    "contract": "TSLA261023C00400000",
                    "strike": 400,
                    "expiry": "2026-10-23",
                    "right": "C",
                    "ask": 2.4,
                    "score": 70,
                },
            ]
        },
        "echo": {
            "option_flow": {
                "golden": [
                    _flow("NBIS", 180, "2026-10-24", premium=800_000, flow_score=40, ask=4.2),
                    _flow("NBIS", 175, "2026-10-24", premium=200_000, flow_score=20, ask=2.1),
                    _flow("NBIS", 170, "2026-10-24", premium=100_000, flow_score=10, ask=3.4),
                    _flow("NBIS", 190, "2026-10-30", premium=50_000, flow_score=5, ask=1.1),
                    _flow("NBIS", 160, "2026-10-30", right="P", premium=30_000, flow_score=-8, ask=0.9),
                    {
                        "symbol": "NBIS",
                        "tier": "golden",
                        "flags": ["golden"],
                        "contract": "NBIS261023C00051000",
                        "strike": 50,
                        "expiry": "2026-10-30",
                        "right": "C",
                        "ask": 0.15,
                        "spot": 50.01,
                        "dte": 8,
                        "premium_notional": 9_000_000,
                        "flow_score": 99,
                    },
                    _flow("AMD", 200, "2026-10-24", premium=300_000, flow_score=15, ask=1.5),
                ]
            }
        },
    }
    alerts = disp.collect_trade_alerts(snap)
    desks = [(a["desk"], a["symbol"]) for a in alerts]
    assert desks == [("Gold Setup", "TSLA"), ("Golden Flow", "NBIS"), ("Golden Flow", "AMD")]
    gold = alerts[0]["message"]
    assert gold.splitlines()[0] == "⭐ 🟢 SURE WINNER TSLA 400C 10/23 MAX"
    assert "Also 🟢 385C 10/23 · ask $7.60" in gold.splitlines()
    assert "parse_mode" not in gold
    flow = alerts[1]["message"]
    lines = flow.splitlines()
    assert lines[0] == "🟢 GOLDEN FLOW NBIS 180C 10/24"
    assert "Also 🟢 175C 10/24 · ask $2.10" in lines
    assert "Also 🟢 170C 10/24 · ask $3.40" in lines
    assert sum(1 for line in lines if line.startswith("Also ")) == 2
    assert "190C" not in flow
    assert "160P" not in flow
    assert "0.15" not in flow
    assert "50C" not in lines[0]


def test_session_suppresses_same_symbol_until_a_stronger_contract(tmp_path, monkeypatch):
    seen, sent = _prime(tmp_path, monkeypatch)
    first = {
        "echo": {
            "option_flow": {
                "golden": [
                    _flow("NBIS", 180, "2026-10-24", premium=800_000, flow_score=40, ask=4.2),
                    _flow("NBIS", 175, "2026-10-24", premium=200_000, flow_score=20, ask=2.1),
                ]
            }
        }
    }
    r1 = disp.dispatch_snapshot_alerts(first, seen_path=seen)
    assert r1.get("sent") == 1
    assert [(row["desk"], row["symbol"]) for row in r1["results"]] == [("Golden Flow", "NBIS")]
    assert sent[0].splitlines()[0] == "🟢 GOLDEN FLOW NBIS 180C 10/24"

    r2 = disp.dispatch_snapshot_alerts(first, seen_path=seen)
    assert r2.get("sent") == 0
    assert len(sent) == 1

    weaker = {
        "echo": {
            "option_flow": {
                "golden": [
                    _flow("NBIS", 170, "2026-10-24", premium=50_000, flow_score=5, ask=1.0),
                ]
            }
        }
    }
    r3 = disp.dispatch_snapshot_alerts(weaker, seen_path=seen)
    assert r3.get("sent") == 0

    stronger = {
        "echo": {
            "option_flow": {
                "golden": [
                    _flow("NBIS", 200, "2026-10-24", premium=1_500_000, flow_score=80, ask=6.5),
                    _flow("NBIS", 180, "2026-10-24", premium=800_000, flow_score=40, ask=4.2),
                ]
            }
        }
    }
    r4 = disp.dispatch_snapshot_alerts(stronger, seen_path=seen)
    assert r4.get("sent") == 1
    assert sent[-1].splitlines()[0] == "🟢 GOLDEN FLOW NBIS 200C 10/24"
    assert "Also 🟢 180C 10/24 · ask $4.20" in sent[-1].splitlines()


def test_send_cap_counts_symbols_not_contracts(tmp_path, monkeypatch):
    seen, sent = _prime(tmp_path, monkeypatch)
    golden = [
        _flow("NBIS", 180 + i, "2026-10-24", premium=100_000 * (5 - i), flow_score=40 - i, ask=1 + i * 0.1)
        for i in range(5)
    ]
    for i, sym in enumerate(["AMD", "SPX", "XSP", "USO", "ALAB", "RKLB", "TSLA", "AMAT"]):
        golden.append(_flow(sym, 100 + i, "2026-10-24", premium=90_000 - i, flow_score=10, ask=1.2))
    result = disp.dispatch_snapshot_alerts(
        {"echo": {"option_flow": {"golden": golden}}},
        seen_path=seen,
        max_send=8,
    )
    assert result.get("sent") == 8
    symbols = [row["symbol"] for row in result["results"]]
    assert symbols[0] == "NBIS"
    assert symbols.count("NBIS") == 1
    assert len(symbols) == len(set(symbols))
    assert sum(body.splitlines()[0].count("NBIS") for body in sent) == 1
