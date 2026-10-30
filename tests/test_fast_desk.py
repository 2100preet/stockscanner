"""Sniper/Diamond hot path, chase, and the activity labels."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from odte_scanner.alerts.dispatcher import collect_trade_alerts, dispatch_snapshot_alerts
from odte_scanner.fast_desk import fast_pause_sec, in_hot_window, run_fast_desk_cycle
from odte_scanner.live_desk import resolve_webull_live_flags, run_live_desk_cycle
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.chase import chase_action, reprice_limit
from odte_scanner.trading.labels import activity_premium_label
from odte_scanner.trading.webull import WebullBroker
from odte_scanner.ui import PAGE, create_app

CT = ZoneInfo("America/Chicago")


def _ct(hour: int, minute: int, day: str = "2026-10-30") -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=CT)


def _card() -> dict:
    return {
        "symbol": "SPY",
        "send": True,
        "send_order": True,
        "action": "SNIPER",
        "sniper": True,
        "desk": "Sniper",
        "right": "P",
        "strike": 759,
        "expiry": "2026-10-30",
        "dte": 0,
        "ask": 1.22,
        "bid": 1.18,
        "spot": 758.4,
        "contract": "SPY261030P00759000",
        "webull": True,
        "at_ct": "09:15",
        "detected_at": "2026-10-30T14:15:40+00:00",
        "detail": "Sniper · 15-minute opening-range retest.",
    }


def test_hot_window_and_pause_clamp(monkeypatch):
    assert in_hot_window(_ct(8, 29)) is False
    assert in_hot_window(_ct(8, 30)) is True
    assert in_hot_window(_ct(10, 31)) is True
    assert in_hot_window(_ct(10, 40)) is False
    assert in_hot_window(_ct(9, 15, "2026-10-31")) is False
    monkeypatch.setenv("FAST_DESK_PAUSE_SEC", "5")
    assert fast_pause_sec() == 15
    monkeypatch.setenv("FAST_DESK_PAUSE_SEC", "90")
    assert fast_pause_sec() == 30
    monkeypatch.setenv("FAST_DESK_PAUSE_SEC", "20")
    assert fast_pause_sec() == 20


def test_entry_alert_shows_the_bar_and_the_send_clock():
    alerts = collect_trade_alerts({"sniper": {"signals": [_card()]}})
    text = alerts[0]["message"]
    assert text.splitlines()[0].startswith("🎯 SNIPER SPY")
    assert "Signal 9:15 bar · sent 9:15:40 CT" in text
    assert "Sure Winner" not in text
    assert "Gold Setup" not in text


def test_fast_cycle_runs_while_the_focus_scan_is_blocked(monkeypatch):
    import odte_scanner.fast_desk as fast_desk

    assert "run_scan" not in inspect.getsource(fast_desk)
    entered = threading.Event()
    release = threading.Event()

    def block(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.scanner.run_scan", block)
    monkeypatch.setattr(
        "odte_scanner.alert_pulse.pulse_desk_alerts",
        lambda *_a, **_k: {"ok": True, "sent": 0},
    )
    slow = threading.Thread(target=run_live_desk_cycle)
    slow.start()
    assert entered.wait(2)
    routed = {"n": 0}

    def route(_diamond, sniper, _now):
        routed["n"] += 1
        assert sniper["signals"][0]["symbol"] == "SPY"
        return {"submitted_n": 1}

    try:
        started = time.monotonic()
        out = run_fast_desk_cycle(
            now=_ct(9, 20),
            boards=({"signals": []}, {"signals": [_card()]}),
            dispatch=lambda _snapshot: {"ok": True, "sent": 1},
            route=route,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()
        slow.join(5)
    assert elapsed < 1.5
    assert not slow.is_alive()
    assert out["scan"] == "not_used"
    assert out["skipped"] is False
    assert routed["n"] == 1
    assert out["alerts"]["sent"] == 1
    assert "Signal 9:15 bar · sent 9:15:40 CT" in out["sample_alert"]
    assert "Sure Winner" not in (out["sample_alert"] or "")


def test_fast_cycle_stays_quiet_outside_the_window():
    def route(*_args, **_kwargs):
        raise AssertionError("hot route ran outside the window")

    out = run_fast_desk_cycle(
        now=_ct(11, 0),
        boards=({"signals": [_card()]}, {"signals": [_card()]}),
        route=route,
        dispatch=lambda _snapshot: {"sent": 1},
    )
    assert out["skipped"] is True
    assert out["scan"] == "not_used"


def test_alert_is_sent_once(tmp_path):
    snap = {"sniper": {"signals": [_card()]}}
    path = tmp_path / "seen.json"
    first = dispatch_snapshot_alerts(snap, seen_path=path, dry_run=True, prime=False)
    second = dispatch_snapshot_alerts(snap, seen_path=path, dry_run=True, prime=False)
    assert first.get("primed") is not True
    assert "Signal 9:15 bar · sent 9:15:40 CT" in first["would_send"][0]
    assert second["sent"] == 0
    assert not second.get("would_send")


def test_chase_math_reprices_under_the_cap_and_stops_past_it():
    assert chase_action(limit=1.05, ask=1.09, signal_ask=1.05, age_sec=30, past_window=False) == "hold"
    assert chase_action(limit=1.05, ask=1.22, signal_ask=1.05, age_sec=30, past_window=False) == "reprice"
    assert reprice_limit(1.22, 1.05, 1.05) == 1.23
    assert chase_action(limit=1.05, ask=1.40, signal_ask=1.05, age_sec=30, past_window=False) == "cap"
    assert chase_action(limit=1.05, ask=1.06, signal_ask=1.05, age_sec=8 * 60, past_window=False) == "cancel"
    assert chase_action(limit=1.05, ask=1.06, signal_ask=1.05, age_sec=30, past_window=True) == "cancel"


def _broker(tmp_path):
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="paper-1",
        submit_interval_sec=0,
    )
    return broker, AutoTrader(broker, golden_only=True, max_orders_per_sync=4)


def _working_buy(broker, *, started: str, symbol: str = "SPY", desk: str = "sniper"):
    intent = broker.stage(
        desk=desk,
        action="BUY",
        symbol=symbol,
        right="P",
        contract="SPY261030P00759000" if symbol == "SPY" else "QQQ261030C00480000",
        expiry="2026-10-30",
        strike=759 if symbol == "SPY" else 480,
        quantity=1,
        limit_price=1.05,
        reason="sniper fixture",
        meta={"golden_kind": desk, "gate": "passed", "signal_ask": 1.05, "chase_started_at": started},
    )
    intent.status = "submitted"
    intent.created_at = started
    intent.meta["fill_state"] = "working"
    intent.meta["chase_started_at"] = started
    broker.save()
    return intent


def _quote(ask: float) -> dict:
    return {"signals": [{**_card(), "ask": ask, "send_order": False}]}


def test_working_buy_reprices_to_the_live_ask_and_keeps_the_signal_ask(tmp_path, caplog):
    broker, trader = _broker(tmp_path)
    started = "2026-10-30T14:16:00+00:00"
    original = _working_buy(broker, started=started)
    now = _ct(9, 18)
    with caplog.at_level(logging.INFO, logger="odte_scanner.trading.auto_trader"):
        trader.sync_hot(sniper=_quote(1.22), now=now)
    assert original.status == "cancelled"
    assert original.meta.get("chase_abandoned") is not True
    replacement = next(order for order in broker.orders if order.status == "dry_run")
    assert replacement.limit_price == 1.23
    assert replacement.fill_price is None
    assert replacement.meta["signal_ask"] == 1.05
    assert replacement.meta["reprice_count"] == 1
    assert replacement.meta["sniper_flat_ct"] == "10:30"
    assert "reprice sniper SPY" in caplog.text
    replacement.status = "submitted"
    replacement.meta["fill_state"] = "working"
    trader.sync_hot(sniper=_quote(1.40), now=now)
    assert replacement.status == "submitted"
    assert replacement.meta.get("chase_capped") is True
    assert replacement.limit_price == 1.23
    replacement.fill_price = 1.23
    assert trader._lot_entry(replacement, {"cost": 0.90}) == 1.23
    assert trader._gold_stop_price(replacement, "sniper", 1.23, "2026-10-30") == round(1.23 * 0.6, 2)


def test_chase_cancels_at_the_cap_clock_and_does_not_reopen(tmp_path, caplog):
    broker, trader = _broker(tmp_path)
    _working_buy(broker, started="2026-10-30T14:00:00+00:00")
    now = _ct(9, 9)
    with caplog.at_level(logging.INFO, logger="odte_scanner.trading.auto_trader"):
        trader.sync_hot(sniper=_quote(1.06), now=now)
    cancelled = broker.orders[0]
    assert cancelled.status == "cancelled"
    assert cancelled.meta.get("chase_abandoned") is True
    assert "unfilled 8m" in caplog.text
    card = {**_card(), "ask": 1.06, "send_order": True}
    again = trader.sync_hot(sniper={"signals": [card]}, now=now)
    assert [row for row in again["submitted"] if row["action"] == "BUY"] == []


def test_chase_cancels_past_the_entry_window_and_leaves_gold_alone(tmp_path, caplog):
    broker, trader = _broker(tmp_path)
    _working_buy(broker, started="2026-10-30T15:25:00+00:00")
    gold = _working_buy(broker, started="2026-10-30T14:00:00+00:00", symbol="QQQ", desk="sure_winner")
    gold.meta["golden_kind"] = "sure_winner_gold"
    broker.save()
    now = _ct(10, 31)
    with caplog.at_level(logging.INFO, logger="odte_scanner.trading.auto_trader"):
        trader.sync_hot(sniper=_quote(1.05), now=now)
    assert broker.orders[1].status == "cancelled" or any(
        order.symbol == "SPY" and order.status == "cancelled" and order.meta.get("chase_abandoned") for order in broker.orders
    )
    assert gold.status == "submitted"
    assert gold.limit_price == 1.05
    assert "past 10:30 CT entry window" in caplog.text


def test_hot_route_buys_spy_once_and_skips_spx(tmp_path):
    broker, trader = _broker(tmp_path)
    now = _ct(9, 16)
    forced = {**_card(), "symbol": "SPX", "contract": "SPX261030P00759000", "webull": True, "send_order": True}
    first = trader.sync_hot(sniper={"signals": [_card(), forced]}, now=now)
    second = trader.sync_hot(sniper={"signals": [_card(), forced]}, now=now)
    buys = [order for order in broker.orders if order.action == "BUY"]
    assert [order.symbol for order in buys] == ["SPY"]
    assert buys[0].status == "dry_run"
    assert first["submitted_n"] == 1
    assert second["submitted_n"] == 0
    flags = resolve_webull_live_flags({"enabled": True, "dry_run": False, "paper": False})
    assert flags["sandbox"] is True
    assert flags["paper"] is True
    assert flags["host"] == "api.sandbox.webull.com"


def test_activity_labels_and_version(monkeypatch):
    assert activity_premium_label({"status": "submitted", "limit_price": 1.05}) == "Limit (working) $1.05"
    assert activity_premium_label({"status": "filled", "limit_price": 1.05, "fill_price": 1.22}) == "Filled @ $1.22"
    assert activity_premium_label({"status": "partial", "limit_price": 1.30, "fill_price": 1.10}) == (
        "Filled @ $1.10 · Limit (working) $1.30"
    )
    assert "function orderPremiumLabel" in PAGE
    assert "Limit (working)" in PAGE
    assert "Filled @" in PAGE
    assert "Live ask" in PAGE
    assert 'o.limit_price==null?"—"' not in PAGE
    monkeypatch.setenv("GIT_SHA", "abc123def")
    monkeypatch.setenv("DEPLOYED_AT", "2026-10-30T14:25:00Z")
    app = create_app()
    res = app.test_client().get("/api/version")
    assert res.status_code == 200
    body = res.get_json()
    assert body == {
        "sha": "abc123def",
        "deployed_at": "2026-10-30T14:25:00Z",
        "service": "stockscanner",
    }
    blob = json.dumps(body)
    assert "APP_SECRET" not in blob
    assert "WEBULL" not in blob
