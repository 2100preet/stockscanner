"""Tick rounding, strike-specific quotes, and limit versus fill labels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from odte_scanner.alerts.dispatcher import collect_trade_alerts, format_unfilled_cancel
from odte_scanner.alerts.exits import format_exit_alert
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.chase import CHASE_MAX_AGE_SEC, repair_signal_ask, reprice_limit
from odte_scanner.trading.portal_book import annotate_card
from odte_scanner.trading.ticks import (
    initial_buy_limit,
    is_tick_reject,
    normalize_option_limit,
    rereound_after_reject,
)
from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent
from tests.test_fast_desk import _broker, _card, _ct, _working_buy


def test_option_ticks_are_a_penny_under_three_and_a_nickel_at_three():
    assert normalize_option_limit(2.99, side="BUY") == 2.99
    assert normalize_option_limit(3.00, side="BUY") == 3.00
    assert normalize_option_limit(3.55, side="BUY", cap=3.93) == 3.55
    assert normalize_option_limit(3.99, side="BUY", cap=3.99) == 3.95
    assert normalize_option_limit(3.99, side="SELL") == 3.95
    assert normalize_option_limit(0.01, side="SELL") == 0.01
    assert normalize_option_limit(1.50, side="SELL") == 1.50
    # The GOOGL 277.5 put: mid of 2.78 / 2.93 rounds up toward the ask.
    assert initial_buy_limit(2.78, 2.93) == 2.86
    assert initial_buy_limit(None, 2.93) == 2.93
    assert initial_buy_limit(3.70, 3.93) == 3.85
    assert rereound_after_reject(3.99, side="BUY") == 3.95
    assert rereound_after_reject(3.95, side="BUY") == 3.90
    assert is_tick_reject(RuntimeError("price increment must be a multiple of 0.05"))
    assert not is_tick_reject(RuntimeError("ACCOUNT_ACCESS_DENIED"))


def test_strike_differs_drops_the_other_contract_quotes():
    """280 put bid/ask must not render on the 277.5 tile. The limit is a limit."""
    order = WebullOrderIntent(
        id="wb-8e90104530",
        desk="sniper",
        action="BUY",
        symbol="GOOGL",
        right="P",
        contract="GOOGL261104P00277500",
        expiry="2026-11-04",
        strike=277.5,
        quantity=1,
        limit_price=2.49,
        status="submitted",
        meta={"golden_kind": "sniper", "signal_ask": 2.49},
    )
    card = annotate_card(
        {
            "symbol": "GOOGL",
            "strike": 280.0,
            "right": "P",
            "expiry": "2026-11-04",
            "bid": 3.55,
            "ask": 3.75,
            "contract": "GOOGL261104P00280000",
        },
        [order.to_dict()],
        "sniper",
    )
    assert card["strike_differs"] is True
    assert card["traded_strike"] == 277.5
    assert card["signal_strike"] == 280.0
    assert card["bid"] is None
    assert card["ask"] is None
    assert card["display_price"] == 2.49
    assert card["display_price_label"] == "Limit (working)"
    assert "Ask" not in card["display_price_label"]

    live = annotate_card(
        {
            "symbol": "GOOGL",
            "strike": 280.0,
            "right": "P",
            "expiry": "2026-11-04",
            "bid": 3.55,
            "ask": 3.75,
            "live_bid": 2.78,
            "live_ask": 2.93,
        },
        [order.to_dict()],
        "sniper",
    )
    assert live["bid"] == 2.78
    assert live["ask"] == 2.93
    assert live["display_price_label"] == "Limit (working)"


def test_entry_alert_labels_the_live_market_and_the_limit_separately():
    card = _card()
    card["symbol"] = "GOOGL"
    card["strike"] = 277.5
    card["signal_strike"] = 280.0
    card["strike_differs"] = True
    card["right"] = "P"
    card["expiry"] = "2026-11-04"
    card["bid"] = None
    card["ask"] = None
    card["live_bid"] = 2.78
    card["live_ask"] = 2.93
    card["spot"] = 277.50
    card["contract"] = "GOOGL261104P00277500"
    card["order_status"] = "submitted"
    card["order_limit"] = 2.86
    card["at_ct"] = "09:30"
    card["detected_at"] = "2026-11-03T14:30:52+00:00"
    # refresh would hit the network; the send path uses the quote already stamped
    # for this contract. A missed fetch must not put the 280 put back.
    alerts = collect_trade_alerts({"sniper": {"signals": [card]}})
    text = alerts[0]["message"]
    assert "Bid $2.78 / Ask $2.93" in text
    assert "Limit $2.86 (working)" in text
    assert "Ask $2.86" not in text
    assert "Ask $2.49" not in text
    assert "3.75" not in text
    assert "3.55" not in text
    assert "277.5P" in text
    assert "Sure Winner" not in text
    assert "SURE WINNER" not in text


def test_filled_entry_says_filled_at_and_not_ask():
    card = _card()
    card["order_status"] = "filled"
    card["order_fill"] = 1.22
    alerts = collect_trade_alerts({"sniper": {"signals": [card]}})
    text = alerts[0]["message"]
    assert "Bid $1.18 / Ask $1.22" in text
    assert "Filled @ $1.22" in text
    assert "Limit $" not in text


def test_exit_pending_is_a_limit_and_a_fill_is_filled_at():
    pending = format_exit_alert(
        tier="sniper",
        symbol="GOOGL",
        strike=277.5,
        right="P",
        expiry="2026-11-04",
        contracts=1,
        entry=2.86,
        entry_at="9:32",
        exit_price=3.40,
        exit_at="10:05",
        reason="take profit +25% ≥ +25%",
        tally="Today 0W/0L · +$0",
        pending=True,
    )
    assert "Exit Limit $3.40 (working)" in pending
    assert "Entry Filled @ $2.86" in pending
    assert "Ask $" not in pending
    filled = format_exit_alert(
        tier="sniper",
        symbol="GOOGL",
        strike=277.5,
        right="P",
        expiry="2026-11-04",
        contracts=1,
        entry=2.86,
        entry_at="9:32",
        exit_price=3.40,
        exit_at="10:05",
        reason="take profit +19% ≥ +25%",
        tally="",
        pending=False,
    )
    assert "Exit Filled @ $3.40" in filled
    assert "Limit $" not in filled


def test_reprice_walks_up_to_the_ask_then_cancels(tmp_path, monkeypatch):
    broker, trader = _broker(tmp_path)
    now = _ct(9, 32)
    started = (now - timedelta(seconds=15)).astimezone(timezone.utc).isoformat()
    order = _working_buy(broker, started=started, symbol="GOOGL")
    order.symbol = "GOOGL"
    order.right = "P"
    order.strike = 277.5
    order.expiry = "2026-11-04"
    order.contract = "GOOGL261104P00277500"
    order.limit_price = 2.49
    order.meta["signal_ask"] = 2.49
    order.meta.pop("signal_ask_live", None)
    broker.save()

    def quote(_row):
        return {"bid": 2.78, "ask": 2.93, "strike": 277.5, "expiry": "2026-11-04", "right": "P", "source": "tradier"}

    monkeypatch.setattr("odte_scanner.options.contract_quote.fetch_contract_quote", quote)
    notes: list[str] = []
    monkeypatch.setattr(
        "odte_scanner.alerts.dispatcher.send_telegram_text",
        lambda body, **_k: notes.append(body) or {"ok": True},
    )
    trader.sync_hot(sniper={"signals": []}, now=now)
    replacement = next(row for row in broker.orders if row.status == "dry_run" and row.symbol == "GOOGL")
    assert replacement.limit_price == 2.93
    assert replacement.meta["signal_ask"] == 2.93
    assert repair_signal_ask(2.49, 2.93, 2.78, live=False) == (2.93, True)
    assert notes == []

    replacement.status = "submitted"
    replacement.meta["fill_state"] = "working"
    later = now + timedelta(seconds=CHASE_MAX_AGE_SEC)
    trader.sync_hot(sniper={"signals": []}, now=later)
    assert replacement.status == "cancelled"
    assert "not filled — cancelled" in str(replacement.meta.get("cancel_reason"))
    assert notes and notes[0] == format_unfilled_cancel(replacement.to_dict(), "sniper")
    assert "not filled — cancelled" in notes[0]
    assert "Limit $" in notes[0]
    assert "Ask $" not in notes[0]
    assert "Sure Winner" not in notes[0]


def test_tick_reject_is_re_rounded_and_retried_once(tmp_path, monkeypatch):
    from tests.session_clock import session_et

    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        account_id="acct",
        ledger_path=tmp_path / "wb.json",
        submit_interval_sec=0,
    )
    broker._session_now = session_et(10, 0)
    seen: list[float] = []

    def place(intent):
        seen.append(float(intent.limit_price))
        if len(seen) == 1:
            raise RuntimeError("tick size: multiple of 0.05 required")
        return "broker-1"

    monkeypatch.setattr(broker, "_place_via_openapi", place)
    intent = broker.stage(
        desk="sniper",
        action="BUY",
        symbol="GOOGL",
        right="P",
        contract="GOOGL261104P00280000",
        expiry="2026-11-04",
        strike=280,
        quantity=1,
        limit_price=3.99,
        meta={"golden_kind": "sniper", "ask_cap": 3.99},
    )
    broker.submit(intent)
    assert seen == [3.95, 3.90]
    assert intent.status == "submitted"
    assert intent.limit_price == 3.90
    assert intent.meta["tick_retried"] is True
    assert broker.status()["ready_live"] is False
    assert broker.status()["paper"] is True
    assert broker.status()["sandbox"] is True
