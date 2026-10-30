"""Sure Winner exits: one config spec for the card and the sell engine."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from odte_scanner.config import load_config
from odte_scanner.signals.exit_rules import (
    gold_exit_plan_text,
    load_exit_rules,
    plan_gold_exit,
)
from odte_scanner.signals.hold_rules import past_gold_flatten
from odte_scanner.signals.sure_winners import _to_sure_winner
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.golden import golden_signal_kind
from odte_scanner.trading.webull import SANDBOX_TRADE_HOST, WebullBroker, build_option_place_order
from tests.session_clock import iso_day, session_et


def _spec():
    cfg = load_config()
    rules = load_exit_rules((cfg.get("live_trading") or {}).get("exits"))
    return rules["sure_winner"].gold


def _broker(tmp_path: Path, name: str = "wb.json", *, live: bool = False) -> WebullBroker:
    return WebullBroker(
        enabled=True,
        dry_run=not live,
        app_key="k" if live else None,
        app_secret="s" if live else None,
        ledger_path=tmp_path / name,
        account_id="margin-1",
        account_id_2="cash-2",
        submit_interval_sec=0,
    )


def _trader(broker: WebullBroker) -> AutoTrader:
    return AutoTrader(
        broker,
        golden_only=True,
        max_orders_per_sync=6,
        max_golden_buys_per_sync=2,
        second_desk="challenge",
    )


def _pos(symbol, strike, expiry, *, cost, mark, quantity=1, right="C", position_id=None):
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    row = {
        "contract": f"{symbol}{yymmdd}{right}{int(round(strike * 1000)):08d}",
        "symbol": symbol,
        "right": right,
        "expiry": expiry,
        "strike": strike,
        "quantity": quantity,
        "cost": cost,
        "mark": mark,
    }
    if position_id:
        row["position_id"] = position_id
    return row


def _arm(broker, pos, *, qty=None, peak=None, exit_plan=None, status="filled"):
    meta = {"original_qty": qty or pos["quantity"], "gate": "passed", "golden_kind": "sure_winner_gold"}
    if peak is not None:
        meta["peak_mark"] = peak
        meta["trail_armed"] = True
    if exit_plan:
        meta["exit_plan"] = exit_plan
    intent = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol=pos["symbol"],
        right=pos["right"],
        contract=pos["contract"],
        expiry=pos["expiry"],
        strike=pos["strike"],
        quantity=qty or pos["quantity"],
        limit_price=pos["cost"],
        reason="gold fixture",
        meta=meta,
    )
    intent.status = status
    intent.fill_price = pos["cost"]
    broker.save()
    return intent


def _install(monkeypatch, broker, rows):
    def positions(account_id=None):
        if account_id == "cash-2":
            return []
        return rows()

    monkeypatch.setattr(broker, "list_option_positions", positions)


def _sync(trader, now):
    return trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=now,
    )


def _sells(out):
    return [row for row in out["submitted"] if row["action"] == "SELL"]


def test_card_text_matches_the_config_spec():
    spec = _spec()
    assert spec is not None
    assert spec.arm_pct == 30
    assert spec.runner_target_pct == 80
    assert spec.stop_loss_pct == 50
    assert spec.same_day_stop_pct == 40
    assert spec.flatten_ct == "14:30"
    same_day = gold_exit_plan_text(spec, dte=0, same_day=True)
    weekly = gold_exit_plan_text(spec, dte=3, same_day=False)
    assert "+30%" in same_day and "−40%" in same_day and "2:30 PM CT" in same_day
    assert "sell half at +30%" in weekly and "+80%" in weekly and "−50%" in weekly
    card = _to_sure_winner(
        {
            "symbol": "WFC",
            "confidence_tier": "HIGH",
            "right": "C",
            "strike": 83,
            "expiry": "2026-10-30",
            "dte": 0,
            "ask": 1.07,
            "contract": "WFC261030C00083000",
            "win_pct": 100,
            "win_samples": 4,
            "exit_plan": "EXIT at ≥$1.71 (+60% premium), or stop −45%, or after 20d max hold",
        },
        day="2026-10-27",
        gold_exit=spec,
    )
    assert card.exit_plan == same_day
    assert "+60%" not in card.exit_plan
    assert "−45%" not in card.exit_plan
    decision = plan_gold_exit(
        entry=1.0,
        mark=1.30,
        held=1,
        original=1,
        spec=spec,
        same_day=True,
        short_dated=True,
    )
    assert decision is not None and decision.quantity == 1
    assert f"+{spec.arm_pct:.0f}%" in decision.detail


def test_one_lot_trail_and_same_day_giveback(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    pos = _pos("TSLA", 410, weekly, cost=2.2, mark=2.86, quantity=1)
    _arm(broker, pos, peak=2.86)
    _install(monkeypatch, broker, lambda: [pos])
    # At the peak the 1-lot stays. The lock is entry + half the gain.
    assert _sells(_sync(trader, morning)) == []
    pos["mark"] = 2.54
    assert _sells(_sync(trader, morning)) == []
    pos["mark"] = 2.53
    trailed = _sells(_sync(trader, morning))
    assert len(trailed) == 1
    assert trailed[0]["quantity"] == 1
    assert "half the peak gain" in trailed[0]["reason"]

    same = _broker(tmp_path, "same.json")
    same_trader = _trader(same)
    today = iso_day(morning)
    lot = _pos("SLV", 48.5, today, cost=1.0, mark=1.20, quantity=1)
    _arm(same, lot, peak=1.40)
    _install(monkeypatch, same, lambda: [lot])
    sold = _sells(_sync(same_trader, morning))
    assert len(sold) == 1
    assert sold[0]["quantity"] == 1
    assert "trail" in sold[0]["reason"]


def test_short_dated_sells_all_and_weekly_sells_half(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    today = iso_day(morning)
    nxt = iso_day(session_et(11, 0, weekday_offset=1))
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    zero = _pos("QQQ", 500, today, cost=1.0, mark=1.30, quantity=3)
    one = _pos("SPY", 580, nxt, cost=1.0, mark=1.30, quantity=2)
    two = _pos("WFC", 83, weekly, cost=1.07, mark=1.40, quantity=2)
    _arm(broker, zero)
    _arm(broker, one)
    _arm(broker, two)
    _install(monkeypatch, broker, lambda: [zero, one, two])
    sells = {row["symbol"]: row for row in _sells(_sync(trader, morning))}
    assert sells["QQQ"]["quantity"] == 3
    assert "+30%" in sells["QQQ"]["reason"]
    assert sells["SPY"]["quantity"] == 2
    assert "scale" not in sells["SPY"]["reason"]
    assert sells["WFC"]["quantity"] == 1
    assert "scale out +30%" in sells["WFC"]["reason"]
    assert "sell 1 of 2" in sells["WFC"]["reason"]


def test_same_day_stop_is_tighter_and_clock_is_230_ct(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    today = iso_day(morning)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    same = _pos("QQQ", 500, today, cost=1.0, mark=0.61, quantity=1)
    swing = _pos("JPM", 340, weekly, cost=1.0, mark=0.60, quantity=1)
    _arm(broker, same)
    _arm(broker, swing)
    _install(monkeypatch, broker, lambda: [same, swing])
    assert _sells(_sync(trader, morning)) == []
    same["mark"] = 0.60
    sells = _sells(_sync(trader, morning))
    assert len(sells) == 1
    assert sells[0]["symbol"] == "QQQ"
    assert "−40%" in sells[0]["reason"]

    flat = _broker(tmp_path, "flat.json")
    flat_trader = _trader(flat)
    held = _pos("IWM", 220, today, cost=1.0, mark=1.10, quantity=1)
    week = _pos("JPM", 347.5, weekly, cost=1.0, mark=1.10, quantity=3)
    _arm(flat, held)
    _arm(flat, week)
    _install(monkeypatch, flat, lambda: [held, week])
    assert past_gold_flatten(session_et(15, 29)) is False
    assert past_gold_flatten(session_et(15, 30)) is True
    assert _sells(_sync(flat_trader, session_et(15, 29))) == []
    closed = _sells(_sync(flat_trader, session_et(15, 30)))
    assert [row["symbol"] for row in closed] == ["IWM"]
    assert "2:30 PM CT" in closed[0]["reason"]
    assert "JPM" not in {row["symbol"] for row in closed}


def test_stale_exit_plan_does_not_override_the_spec(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path)
    trader = _trader(broker)
    pos = _pos("JPM", 342.5, weekly, cost=1.0, mark=0.60, quantity=1)
    _arm(
        broker,
        pos,
        exit_plan="EXIT at ≥$1.60 (+60% premium), or stop −45%, or after 20d max hold",
    )
    _install(monkeypatch, broker, lambda: [pos])
    assert _sells(_sync(trader, morning)) == []
    pos["mark"] = 0.50
    sells = _sells(_sync(trader, morning))
    assert len(sells) == 1
    assert "−50%" in sells[0]["reason"]


def test_stale_working_sell_is_replaced_and_a_flat_book_is_not(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "live.json", live=True)
    trader = _trader(broker)
    pos = _pos("QUBT", 6.5, weekly, cost=0.20, mark=0.08, quantity=1, position_id="pos-qubt")
    _arm(broker, pos)
    stale = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="QUBT",
        right="C",
        contract=pos["contract"],
        expiry=weekly,
        strike=6.5,
        quantity=3,
        limit_price=0.15,
        reason="old stop",
        meta={"gate": "exit"},
    )
    stale.status = "submitted"
    broker.save()
    cancelled: list[str] = []
    placed: list = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "3"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-1")
    _install(monkeypatch, broker, lambda: [pos])
    _sync(trader, morning)
    assert cancelled == [stale.id]
    assert len(placed) == 1
    assert placed[0].quantity == 1
    assert placed[0].limit_price == 0.10
    body = build_option_place_order(placed[0])
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["legs"][0]["position_intent"] == "SELL_TO_CLOSE"
    assert body["quantity"] == "1"
    assert "position_id" not in body
    assert placed[0].meta["host"] == SANDBOX_TRADE_HOST

    flat_broker = _broker(tmp_path, "flat-live.json", live=True)
    flat_trader = _trader(flat_broker)
    gone = _pos("SLV", 48.5, weekly, cost=0.17, mark=0.05, quantity=0)
    buy = _arm(flat_broker, {**gone, "quantity": 1})
    buy.status = "filled"
    rejected = flat_broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="SLV",
        right="C",
        contract=gone["contract"],
        expiry=weekly,
        strike=48.5,
        quantity=1,
        limit_price=0.08,
        reason="naked reject",
    )
    rejected.status = "rejected"
    rejected.error = "OPENAPI_TICKER_ID_NOT_SUPPORT_NAKED_EQUTY_OPTION_WITH_NUM"
    flat_broker.save()
    placed.clear()
    monkeypatch.setattr(flat_broker, "_place_via_openapi", lambda order: placed.append(order) or "NO")
    _install(monkeypatch, flat_broker, lambda: [])
    out = _sync(flat_trader, morning)
    assert _sells(out) == []
    assert placed == []


def test_rejected_sell_retries_only_against_a_live_long(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=4))
    broker = _broker(tmp_path, "retry.json")
    trader = _trader(broker)
    pos = _pos("WFC", 83, weekly, cost=1.07, mark=0.50, quantity=2)
    _arm(broker, pos)
    rejected = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="WFC",
        right="C",
        contract=pos["contract"],
        expiry=weekly,
        strike=83,
        quantity=5,
        limit_price=0.40,
        reason="rejected stop",
    )
    rejected.status = "rejected"
    broker.save()
    _install(monkeypatch, broker, lambda: [pos])
    sells = _sells(_sync(trader, morning))
    assert len(sells) == 1
    assert sells[0]["quantity"] == 2
    assert sells[0]["status"] == "dry_run"
    body = build_option_place_order(next(o for o in broker.orders if o.action == "SELL" and o.status == "dry_run"))
    assert body["position_intent"] == "SELL_TO_CLOSE"
    assert body["quantity"] == "2"


def test_night_pulse_does_not_flatten_open_weeklies_or_touch_working_sells(tmp_path: Path, monkeypatch):
    night = session_et(21, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path, "night.json")
    trader = _trader(broker)
    names = [
        ("WFC", 83, 1.07, 2),
        ("TSLA", 410, 2.2, 1),
        ("JPM", 347.5, 0.42, 3),
        ("JPM", 340, 2.03, 1),
        ("JPM", 342.5, 0.84, 2),
        ("QUBT", 6.5, 0.15, 2),
    ]
    rows = []
    for symbol, strike, cost, qty in names:
        right = "P" if symbol == "QUBT" and cost == 0.15 else "C"
        pos = _pos(symbol, strike, weekly, cost=cost, mark=cost * 1.4, quantity=qty, right=right)
        if symbol == "QUBT" and right == "C":
            pos = _pos("QUBT", 6.5, weekly, cost=0.20, mark=0.26, quantity=3, right="C")
        _arm(broker, pos, peak=pos["mark"])
        rows.append(pos)
    working = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol=rows[0]["symbol"],
        right="C",
        contract=rows[0]["contract"],
        expiry=weekly,
        strike=rows[0]["strike"],
        quantity=1,
        limit_price=0.90,
        reason="stuck submitted sell",
    )
    working.status = "submitted"
    broker.save()
    cancelled: list[str] = []
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    _install(monkeypatch, broker, lambda: rows)
    out = _sync(trader, night)
    assert _sells(out) == []
    assert cancelled == []
    assert working.status == "submitted"


def test_expired_contract_is_not_resold(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    yesterday = (morning - timedelta(days=1)).date().isoformat()
    broker = _broker(tmp_path, "old.json", live=True)
    trader = _trader(broker)
    pos = _pos("MRK", 90, yesterday, cost=1.0, mark=0.20, quantity=1)
    _arm(broker, pos, status="filled")
    stale = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="MRK",
        right="C",
        contract=pos["contract"],
        expiry=yesterday,
        strike=90,
        quantity=1,
        limit_price=0.40,
        reason="expired stop",
    )
    stale.status = "submitted"
    broker.save()
    cancelled: list[str] = []
    placed: list = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "NO")
    _install(monkeypatch, broker, lambda: [pos])
    assert _sells(_sync(trader, morning)) == []
    assert placed == []
    assert cancelled == [stale.id]


def test_golden_flow_is_not_a_webull_route(tmp_path: Path):
    broker = _broker(tmp_path, "flow.json")
    trader = _trader(broker)
    flow = {
        "symbol": "NBIS",
        "right": "C",
        "premium": 250000,
        "tier": "GOLDEN",
        "unusual": True,
        "ask": 1.2,
        "contract": "NBIS261030C00080000",
        "expiry": "2026-10-30",
        "strike": 80,
    }
    assert golden_signal_kind(flow) is None
    out = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        option_flow={"prints": [flow]},
        now=session_et(11, 0),
    )
    assert out["submitted"] == []
