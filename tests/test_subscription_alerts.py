"""Paid Gold Setup / Golden Flow alerts and the free Morning Spark digest."""

from __future__ import annotations

from datetime import datetime, timezone

from odte_scanner.alerts.subscription import (
    collect_subscription_signals,
    dispatch_subscription_alerts,
    format_gold_setup,
    format_golden_flow,
    format_morning_spark,
    in_digest_window,
    publish_morning_digest,
)
from odte_scanner.billing.copy import DISCLAIMER
from odte_scanner.trading.golden import golden_signal_kind


def _gold():
    return {
        "symbol": "NVDA",
        "right": "C",
        "confidence_tier": "MAX",
        "sure_winner": True,
        "strike": 120,
        "expiry": "2026-08-21",
        "contract": "NVDA260821C00120000",
        "signaled_at": "2026-10-14T15:00:00+00:00",
        "detail": "Sure Winner internal note",
    }


def _solid():
    return {
        "symbol": "AMD",
        "right": "C",
        "confidence_tier": "SOLID",
        "sure_winner": True,
        "strike": 160,
        "expiry": "2026-08-21",
    }


def _flow():
    return {
        "symbol": "SPY",
        "right": "C",
        "tier": "golden",
        "flags": ["golden"],
        "strike": 500,
        "expiry": "2026-08-07",
        "contract": "SPY260807C00500000",
        "premium_notional": 250000,
        "ask": 2.1,
    }


def _snapshot():
    return {
        "sure_winners": {"winners": [_gold(), _solid()], "buy_now": [_gold()]},
        "echo": {"option_flow": {"golden": [_flow()], "prints": [_flow(), {"symbol": "QQQ", "tier": "unusual"}]}},
        "challenge": {"earnings_watch": [{"symbol": "AAPL", "days_to_earnings": 1, "next_earnings": "2026-10-16"}]},
        "watch": {"quotes": {"TSLA": {"change_pct": 3.2}, "AAPL": {"change_pct": -1.1}}},
    }


def test_alerts_are_impersonal_and_do_not_use_the_old_name():
    text = format_gold_setup(_gold())
    flow = format_golden_flow(_flow())
    assert "Gold Setup" in text
    assert "Golden Flow" in flow
    assert "Sure Winner" not in text
    assert "Sure Winner" not in flow
    assert DISCLAIMER in text and DISCLAIMER in flow
    assert "2.1" not in flow
    assert "250000" not in flow
    assert "you should" not in text.lower()
    assert "instruction to buy or sell" in text.lower()
    signals = collect_subscription_signals(_snapshot())
    kinds = {(item["kind"], item["symbol"]) for item in signals}
    assert kinds == {("gold_setup", "NVDA"), ("golden_flow", "SPY")}
    # Golden flow is an alert, and it is still not a Webull route.
    assert golden_signal_kind(_flow()) is None
    assert golden_signal_kind(_gold()) == "sure_winner_gold"


def test_dispatch_primes_then_posts_to_paid_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_PAID_CHANNEL_ID", "-100555")
    monkeypatch.delenv("SUBSCRIPTION_ALERTS_SEND_EXISTING", raising=False)
    seen = tmp_path / "seen.json"
    sent = []

    def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.alerts.subscription.send_channel_message", fake_send)
    first = dispatch_subscription_alerts(_snapshot(), seen=seen)
    assert first["primed"] is True
    assert first["sent"] == 0
    assert sent == []
    second = dispatch_subscription_alerts(_snapshot(), seen=seen)
    assert second["sent"] == 0
    fresh = _snapshot()
    fresh["sure_winners"]["winners"].append({
        "symbol": "META",
        "right": "P",
        "confidence_tier": "HIGH",
        "sure_winner": True,
        "strike": 500,
        "expiry": "2026-11-20",
        "signaled_at": "2026-10-15T15:00:00+00:00",
    })
    third = dispatch_subscription_alerts(fresh, seen=seen)
    assert third["sent"] == 1
    assert sent[0][0] == "-100555"
    assert "META" in sent[0][1]
    assert "Sure Winner" not in sent[0][1]
    assert DISCLAIMER in sent[0][1]


def test_morning_spark_posts_once_to_the_public_channel(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_FREE_CHANNEL_ID", "@morning")
    monkeypatch.setenv("MORNING_SPARK_PATH", str(tmp_path / "spark.json"))
    # Thursday 15 Oct 2026 13:00 UTC is 8:00 AM Chicago (CDT).
    morning = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)
    overnight = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    assert in_digest_window(morning) is True
    assert in_digest_window(overnight) is False
    assert in_digest_window(datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)) is False
    sent = []

    def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.alerts.subscription.send_channel_message", fake_send)
    early = publish_morning_digest(_snapshot(), now=overnight)
    assert early["skipped"] is True
    assert sent == []
    posted = publish_morning_digest(_snapshot(), now=morning)
    assert posted["sent"] == 1
    assert sent[0][0] == "@morning"
    assert "Morning Spark" in sent[0][1]
    assert "NVDA" in sent[0][1]
    assert "AAPL" in sent[0][1]
    assert "TSLA" in sent[0][1]
    assert "Sure Winner" not in sent[0][1]
    assert DISCLAIMER in sent[0][1]
    again = publish_morning_digest(_snapshot(), now=morning)
    assert again["skipped"] is True
    assert len(sent) == 1
    text = format_morning_spark(_snapshot(), now=morning)
    assert "delayed" in text
