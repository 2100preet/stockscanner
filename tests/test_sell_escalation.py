"""Rejected sells: re-check the position, walk the price down, show the reason."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401  (import order: avoids a diamond/edge cycle)
from odte_scanner.alerts.exits import collect_reject_alerts
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.portal_book import public_order, status_label
from odte_scanner.trading.webull import WebullBroker, parse_webull_error
from tests.session_clock import iso_day, session_et

REVERSE = ("HTTP Status: 417, Code: OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION, Msg: This order cannot be entered "
           "because it will reverse an existing position., RequestID: abc")
GENERIC = "HTTP Status: 417, Code: OPENAPI_SOMETHING_ELSE, Msg: Order failed., RequestID: def"
HOURS = ("HTTP Status: 417, Code: OPENAPI_OPTION_ONLY_SUPPORT_MARKET_IN_CORE_TIME, Msg: You cannot place orders "
         "at this moment., RequestID: x")


def _occ(symbol: str, expiry: str, right: str, strike: float) -> str:
    return f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{right}{int(round(strike * 1000)):08d}"


def _setup(tmp_path: Path, monkeypatch, error: str, *, held=True):
    now = session_et(11, 31)  # 10:31 CT
    expiry = iso_day(now)
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id="a",
                          ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    broker._fetch_order_detail = lambda intent: {"status": "FILLED", "filled_quantity": 1, "total_quantity": 1}  # type: ignore
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    contract = _occ("SPY", expiry, "C", 746)
    buy = broker.stage(desk="sniper", action="BUY", symbol="SPY", contract=contract, expiry=expiry, strike=746,
                       limit_price=2.76, meta={"golden_kind": "sniper"})
    buy.status = "filled"
    buy.fill_price = 2.76
    buy.broker_order_id = "B"
    buy.created_at = (now - timedelta(minutes=90)).isoformat()
    broker.save()
    state = {"held": held}
    pos = {"contract": contract, "symbol": "SPY", "right": "C", "expiry": expiry, "strike": 746, "quantity": 1,
           "cost": 2.76, "bid": 3.10, "mark": 3.10}
    monkeypatch.setattr(broker, "list_option_positions",
                        lambda account_id=None: [dict(pos)] if state["held"] else [])
    sent: list[float] = []

    def place(intent):
        if intent.action == "SELL":
            sent.append(float(intent.limit_price))
            raise RuntimeError(error)
        return "X"

    broker._place_via_openapi = place  # type: ignore[method-assign]
    import odte_scanner.trading.auto_trader as at
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    return now, broker, trader, buy, state, sent


def test_parse_webull_error_drops_request_id():
    code, msg = parse_webull_error(REVERSE)
    assert code == "OPENAPI_ORDER_NOT_SUPPORT_REVERSE_OPTION"
    assert "reverse an existing position" in msg and "RequestID" not in msg


def test_held_rejects_walk_to_bid_then_one_cent(tmp_path: Path, monkeypatch):
    now, broker, trader, buy, _state, sent = _setup(tmp_path, monkeypatch, GENERIC)
    for step in range(3):
        trader.sync(now=now + timedelta(seconds=10 * step))
    assert len(sent) == 3
    assert sent[1] <= 3.10 + 1e-9  # second try at (or under) the bid
    assert sent[2] == 0.01  # third try is the marketable floor
    rejected = [o for o in broker.orders if o.action == "SELL"]
    assert all(o.status == "rejected" for o in rejected)
    assert all(o.meta.get("reject_kind") == "held" for o in rejected)
    assert rejected[0].meta.get("next_step")


def test_no_position_reject_stops_the_retry_loop(tmp_path: Path, monkeypatch):
    now, broker, trader, buy, state, sent = _setup(tmp_path, monkeypatch, REVERSE)
    original = broker.list_option_positions
    calls = {"n": 0}

    def positions(account_id=None):
        calls["n"] += 1
        # Held at the start of the pass, gone when the reject is re-checked.
        return original(account_id) if calls["n"] == 1 else []

    monkeypatch.setattr(broker, "list_option_positions", positions)
    trader.sync(now=now)
    assert len(sent) == 1
    assert buy.meta.get("position_gone") is True
    sell = next(o for o in broker.orders if o.action == "SELL")
    assert sell.meta["reject_kind"] == "no_position"
    state["held"] = False
    trader.sync(now=now + timedelta(seconds=10))
    assert len(sent) == 1


def test_outside_hours_reject_is_queued_not_rejected(tmp_path: Path, monkeypatch):
    now, broker, trader, buy, _state, sent = _setup(tmp_path, monkeypatch, HOURS)
    trader.sync(now=now)
    sell = next(o for o in broker.orders if o.action == "SELL")
    assert sell.status == "deferred" and sell.meta["queued_for_open"] is True


def test_portal_shows_rejected_with_reason():
    order = {"id": "wb-1", "action": "SELL", "status": "rejected", "error": REVERSE, "meta": {}}
    assert status_label(order) == "rejected"
    pub = public_order(order)
    assert pub["status_label"] == "rejected"
    assert "reverse an existing position" in pub["error"]
    assert "RequestID" not in pub["error"]


def test_reject_alert_line_labels_the_limit(tmp_path: Path, monkeypatch):
    now, broker, trader, buy, _state, _sent = _setup(tmp_path, monkeypatch, GENERIC)
    trader.sync(now=now)
    alerts = collect_reject_alerts(broker.orders, now=now, seen=set())
    assert len(alerts) == 1
    msg = alerts[0]["message"]
    assert "SNIPER EXIT REJECTED" in msg and "Limit $" in msg and "Webull: Order failed." in msg
    assert "Sure Winner" not in msg
    again = collect_reject_alerts(broker.orders, now=now, seen={alerts[0]["order_id"]})
    assert again == []
