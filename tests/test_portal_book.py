"""Portal tiles follow the Webull paper fill, and the P&L matches the exit alert."""

from __future__ import annotations

from datetime import datetime, timezone

from odte_scanner.alerts.exits import collect_exit_alerts
from odte_scanner.hot_state import apply_hot_overlay, clear_hot, publish_hot
from odte_scanner.trading.portal_book import annotate_card, reconcile_pnl, status_label
from odte_scanner.trading.webull import WebullOrderIntent
from odte_scanner.ui import PAGE


def _orders() -> list[dict]:
    buy = WebullOrderIntent(
        id="buy-spy",
        desk="sniper",
        action="BUY",
        symbol="SPY",
        right="C",
        contract="SPY261102C00760000",
        expiry="2026-11-02",
        strike=760,
        quantity=1,
        limit_price=1.80,
        fill_price=1.80,
        status="filled",
        created_at="2026-11-02T14:50:15+00:00",
        updated_at="2026-11-02T14:50:15+00:00",
        meta={"golden_kind": "sniper", "account_id": "secret-account"},
    )
    sell = WebullOrderIntent(
        id="sell-spy",
        desk="golden_exit",
        action="SELL",
        symbol="SPY",
        right="C",
        contract="SPY261102C00760000",
        expiry="2026-11-02",
        strike=760,
        quantity=1,
        limit_price=2.59,
        fill_price=2.59,
        status="filled",
        reason="take profit +43% ≥ +25%",
        created_at="2026-11-02T15:15:01+00:00",
        updated_at="2026-11-02T15:15:01+00:00",
        meta={"golden_kind": "golden_exit"},
    )
    cancelled = WebullOrderIntent(
        id="buy-amzn",
        desk="sniper",
        action="BUY",
        symbol="AMZN",
        right="C",
        contract="AMZN261102C00250000",
        expiry="2026-11-02",
        strike=250,
        quantity=1,
        limit_price=3.10,
        status="cancelled",
        meta={"golden_kind": "sniper"},
    )
    return [buy.to_dict(), sell.to_dict(), cancelled.to_dict()]


def test_tile_prefers_the_traded_strike_and_the_fill():
    card = annotate_card(
        {"symbol": "SPY", "strike": 759, "right": "C", "expiry": "2026-11-02", "ask": 1.22, "contract": "SPY261102C00759000"},
        _orders(),
        "sniper",
    )
    assert card["traded_strike"] == 760
    assert card["signal_strike"] == 759
    assert card["strike_differs"] is True
    assert card["fill_price"] == 1.80
    assert card["display_price"] == 1.80
    assert card["display_price_label"] == "Filled @"
    assert card["order_status_label"] == "filled"
    assert card["exit_fill"] == 2.59
    assert "secret-account" not in str(card)


def test_working_and_cancelled_labels():
    assert status_label({"status": "submitted"}) == "working limit"
    assert status_label({"status": "cancelled"}) == "cancelled"
    card = annotate_card({"symbol": "AMZN", "strike": 255, "right": "C", "ask": 4.0}, _orders(), "sniper")
    assert card["order_status_label"] == "cancelled"
    assert card["traded_strike"] == 250
    assert card["strike_differs"] is True
    assert card["display_price_label"] == "Limit"


def test_portal_pnl_matches_the_exit_alert():
    orders = _orders()
    report = reconcile_pnl(orders)
    intents = []
    for row in orders:
        if row["symbol"] != "SPY":
            continue
        payload = dict(row)
        meta = payload.pop("meta", {}) or {}
        intents.append(WebullOrderIntent(**payload, meta=meta))
    alerts = collect_exit_alerts(intents, now=datetime(2026, 11, 2, 15, 16, tzinfo=timezone.utc), seen=set())
    assert report["match"] is True
    assert report["fill_pnl"] == 79.0
    assert report["alert_pnl"] == round(sum(alert["pnl"] for alert in alerts), 2)
    assert report["fill_pnl"] == report["alert_pnl"]


def test_overlay_uses_the_fast_board_and_the_ledger():
    clear_hot()
    publish_hot(
        sniper={
            "ok": True,
            "signals": [
                {"symbol": "SPY", "strike": 759, "right": "C", "ask": 1.22, "send": False, "expiry": "2026-11-02"}
            ],
        }
    )
    body = apply_hot_overlay(
        {"sniper": {"signals": [{"symbol": "QQQ", "strike": 500}]}, "sure_winners": {"winners": []}},
        orders=_orders(),
    )
    card = body["sniper"]["signals"][0]
    assert card["symbol"] == "SPY"
    assert card["traded_strike"] == 760
    assert card["display_price"] == 1.80
    assert body["pnl_reconcile"]["fill_pnl"] == 79.0
    assert body["hot_at"]
    blob = str(body["webull_book"])
    assert "secret-account" not in blob
    assert "tradedView" in PAGE
    assert "edgeWebullBook" in PAGE
    clear_hot()
