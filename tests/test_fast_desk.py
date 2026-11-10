"""Sniper/Diamond hot path, chase, and the activity labels."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
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


def test_chase_math_reprices_under_the_cap_and_stops_past_it(monkeypatch):
    from odte_scanner.trading.chase import CHASE_MAX_AGE_SEC

    monkeypatch.delenv("ENTRY_CHASE_MAX_AGE_SEC", raising=False)

    # Already at the ask: hold. Below the ask and inside the cap: reprice.
    assert chase_action(limit=1.22, ask=1.22, signal_ask=1.22, age_sec=30, past_window=False) == "hold"
    assert chase_action(limit=1.05, ask=1.22, signal_ask=1.22, age_sec=30, past_window=False) == "reprice"
    assert reprice_limit(1.22, 1.22, 1.05) == 1.22
    capped = reprice_limit(1.40, 1.22, 1.22)
    assert capped is not None and capped < 1.40
    assert chase_action(limit=capped, ask=1.40, signal_ask=1.22, age_sec=30, past_window=False) == "cap"
    # The entry keeps working for the whole entry window, not 75 s.
    assert chase_action(limit=1.06, ask=1.06, signal_ask=1.05, age_sec=CHASE_MAX_AGE_SEC, past_window=False) == "hold"
    assert chase_action(limit=1.05, ask=1.06, signal_ask=1.05, age_sec=3600, past_window=False) == "reprice"
    assert chase_action(limit=capped, ask=1.40, signal_ask=1.22, age_sec=3600, past_window=False) == "cap"
    assert chase_action(limit=1.05, ask=1.06, signal_ask=1.05, age_sec=30, past_window=True) == "cancel"
    # An age cutoff can still be configured.
    monkeypatch.setenv("ENTRY_CHASE_MAX_AGE_SEC", "75")
    assert chase_action(limit=1.05, ask=1.06, signal_ask=1.05, age_sec=CHASE_MAX_AGE_SEC, past_window=False) == "cancel"
    monkeypatch.setenv("ENTRY_CHASE_MAX_AGE_SEC", "junk")
    assert chase_action(limit=1.06, ask=1.06, signal_ask=1.05, age_sec=3600, past_window=False) == "hold"


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
    now = _ct(9, 18)
    started = (now - timedelta(seconds=20)).astimezone(timezone.utc).isoformat()
    original = _working_buy(broker, started=started)
    with caplog.at_level(logging.INFO, logger="odte_scanner.trading.auto_trader"):
        trader.sync_hot(sniper=_quote(1.22), now=now)
    assert original.status == "cancelled"
    assert original.meta.get("chase_abandoned") is not True
    replacement = next(order for order in broker.orders if order.status == "dry_run")
    # The stored 1.05 was below the 1.18 bid, so the baseline becomes the live ask.
    assert replacement.limit_price == 1.22
    assert replacement.fill_price is None
    assert replacement.meta["signal_ask"] == 1.22
    assert replacement.meta["reprice_count"] == 1
    assert replacement.meta["sniper_flat_ct"] == "10:30"
    assert "reprice sniper SPY" in caplog.text
    replacement.status = "submitted"
    replacement.meta["fill_state"] = "working"
    trader.sync_hot(sniper=_quote(1.40), now=now)
    assert replacement.status == "cancelled"
    chased = next(order for order in broker.orders if order.status == "dry_run")
    assert chased.limit_price == reprice_limit(1.40, 1.22, 1.22)
    assert chased.meta["signal_ask"] == 1.22
    chased.status = "submitted"
    chased.meta["fill_state"] = "working"
    trader.sync_hot(sniper=_quote(1.40), now=now)
    assert chased.status == "submitted"
    assert chased.meta.get("chase_capped") is True
    assert chased.limit_price == reprice_limit(1.40, 1.22, 1.22)
    chased.fill_price = chased.limit_price
    assert trader._lot_entry(chased, {"cost": 0.90}) == chased.limit_price
    assert trader._gold_stop_price(chased, "sniper", chased.limit_price, "2026-10-30") == round(chased.limit_price * 0.6, 2)


def test_chase_keeps_working_then_cancels_at_window_end_and_does_not_reopen(tmp_path, caplog):
    broker, trader = _broker(tmp_path)
    working = _working_buy(broker, started="2026-10-30T14:00:00+00:00")
    working.limit_price = 1.06
    broker.save()
    # 9:09 CT is long after 75 s, but inside the 10:30 CT entry window: still working.
    trader.sync_hot(sniper=_quote(1.06), now=_ct(9, 9))
    assert working.status not in {"cancelled"}
    assert [o for o in broker.orders if o.action == "BUY" and o.status == "cancelled"] == []
    now = _ct(10, 31)
    with caplog.at_level(logging.INFO, logger="odte_scanner.trading.auto_trader"):
        trader.sync_hot(sniper=_quote(1.06), now=now)
    cancelled = next(o for o in broker.orders if o.id == working.id)
    assert cancelled.status == "cancelled"
    assert cancelled.meta.get("chase_abandoned") is True
    assert "past 10:30 CT entry window" in caplog.text
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
    assert "function quoteText" in PAGE
    assert "Bid " in PAGE
    assert "Limit " in PAGE
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
    assert "setInterval(loadAll, window.SIGNAL_DESK_STATIC ? 180000 : 5000);" in PAGE
    assert "function tradedView" in PAGE


def test_routed_sniper_alerts_after_the_window_flag_drops():
    """Today's shape: the order filled, then the card's send flag went false."""
    captured: dict = {}

    def route(_diamond, sniper, _now):
        assert sniper["signals"][0]["send"] is False
        return {
            "submitted": [
                {
                    "symbol": "SPY",
                    "desk": "sniper",
                    "action": "BUY",
                    "status": "filled",
                    "strike": 760,
                    "right": "C",
                    "expiry": "2026-10-30",
                    "contract": "SPY261030C00760000",
                    "fill_price": 1.80,
                    "meta": {"golden_kind": "sniper"},
                }
            ]
        }

    def dispatch(snapshot):
        card = snapshot["sniper"]["signals"][0]
        captured["card"] = card
        captured["alerts"] = collect_trade_alerts(snapshot)
        return {"ok": True, "sent": 1}

    card = _card()
    card["send"] = False
    card["strike"] = 759
    card["ask"] = 1.22
    out = run_fast_desk_cycle(
        now=_ct(9, 20),
        boards=({"signals": []}, {"signals": [card]}),
        dispatch=dispatch,
        route=route,
    )
    assert out["route"]["submitted"][0]["status"] == "filled"
    assert captured["card"]["send"] is True
    assert captured["card"]["routed_order"] is True
    assert captured["card"]["strike"] == 760
    assert captured["card"]["signal_strike"] == 759
    assert captured["alerts"][0]["desk"] == "Sniper"
    text = captured["alerts"][0]["message"]
    assert "760C" in text
    assert "Filled @ $1.80" in text
    assert "Ask $1.80" not in text
    assert "Ask $1.22" not in text
    assert "Signal 9:15 bar · sent 9:15:40 CT" in text
    assert "Sure Winner" not in text


def test_hot_telegram_is_nonblocking_and_logged(monkeypatch, tmp_path):
    import odte_scanner.fast_desk as fast_desk
    from odte_scanner.alert_pulse import read_alert_log

    monkeypatch.setattr("odte_scanner.alerts.dispatcher.DEFAULT_SEEN", tmp_path / "seen.json")
    monkeypatch.setattr("odte_scanner.alert_pulse.alert_log_path", lambda: tmp_path / "alerts.jsonl")
    monkeypatch.setattr(
        "odte_scanner.alerts.dispatcher.telegram_configured",
        lambda: {"ok": True, "token_set": True, "chat_set": True, "enabled": True},
    )
    monkeypatch.setattr("odte_scanner.alerts.dispatcher.whatsapp_configured", lambda: {"ok": False})
    started = {}

    def slow(message):
        started["message"] = message
        time.sleep(1.2)
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.alerts.dispatcher.send_telegram_text", slow)
    t0 = time.monotonic()
    out = run_fast_desk_cycle(
        now=_ct(9, 20),
        boards=({"signals": []}, {"signals": [_card()]}),
        route=lambda *_args: {"submitted_n": 1, "submitted": []},
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.6
    assert out["alerts"]["queued"] is True
    assert fast_desk.last_alert_thread is not None
    fast_desk.last_alert_thread.join(3)
    assert "Signal 9:15 bar · sent 9:15:40 CT" in started["message"]
    log = read_alert_log(path=tmp_path / "alerts.jsonl")
    assert log["alerts"][0]["kind"] == "Sniper"
    assert log["alerts"][0]["symbol"] == "SPY"
    assert log["alerts"][0]["ok"] is True


def test_send_does_not_hold_the_alert_lock(monkeypatch, tmp_path):
    from odte_scanner.alerts.dispatcher import _ALERT_LOCK, dispatch_snapshot_alerts

    monkeypatch.setattr(
        "odte_scanner.alerts.dispatcher.telegram_configured",
        lambda: {"ok": True, "token_set": True, "chat_set": True, "enabled": True},
    )
    monkeypatch.setattr("odte_scanner.alerts.dispatcher.whatsapp_configured", lambda: {"ok": False})
    entered = threading.Event()
    release = threading.Event()

    def slow(_message):
        entered.set()
        assert _ALERT_LOCK.acquire(blocking=False)
        _ALERT_LOCK.release()
        assert release.wait(2)
        return {"ok": True}

    monkeypatch.setattr("odte_scanner.alerts.dispatcher.send_telegram_text", slow)
    worker = threading.Thread(
        target=lambda: dispatch_snapshot_alerts(
            {"sniper": {"signals": [_card()]}},
            seen_path=tmp_path / "seen.json",
            prime=False,
        )
    )
    worker.start()
    assert entered.wait(2)
    release.set()
    worker.join(3)
    assert not worker.is_alive()
