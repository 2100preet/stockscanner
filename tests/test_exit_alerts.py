"""Exit Telegram copy, dedupe, the two-minute fallback, tallies, and the 10:30 retry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import odte_scanner.edge.diamond_study  # noqa: F401  — load Edge before Diamond so sync can import it

from odte_scanner.alerts.exits import (
    collect_exit_alerts,
    dispatch_exit_alerts,
    format_exit_alert,
    plain_exit_reason,
)
from odte_scanner.time_cst import CT
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent, orders_for_ledger
from tests.session_clock import iso_day, session_et


def _intent(**kwargs) -> WebullOrderIntent:
    base = dict(
        id="sell-1",
        desk="golden_exit",
        action="SELL",
        symbol="MSFT",
        right="P",
        contract="MSFT261030P00525000",
        expiry="2026-10-30",
        strike=525,
        quantity=1,
        limit_price=6.65,
        fill_price=6.65,
        reason="take profit +40% ≥ +25%",
        status="filled",
        created_at="2026-10-30T14:13:00+00:00",
        updated_at="2026-10-30T14:47:00+00:00",
        meta={
            "exit_tier": "sniper",
            "entry_price": 4.75,
            "entry_at": "2026-10-30T14:13:00+00:00",
            "exit_clock": "2026-10-30T14:13:00+00:00",
        },
    )
    base.update(kwargs)
    meta = dict(base["meta"])
    if "meta" in kwargs:
        meta.update(kwargs["meta"])
        base["meta"] = meta
    return WebullOrderIntent(**base)


def test_sniper_exit_line_matches_the_customer_copy():
    text = format_exit_alert(
        tier="sniper",
        symbol="MSFT",
        strike=525,
        right="P",
        expiry="2026-10-30",
        contracts=1,
        entry=4.75,
        entry_at=datetime(2026, 10, 30, 9, 13, tzinfo=CT),
        exit_price=6.65,
        exit_at=datetime(2026, 10, 30, 9, 47, tzinfo=CT),
        reason="take profit +40% ≥ +25%",
        tally="Today 1W/0L · +$190",
    )
    assert text == (
        "🎯 SNIPER EXIT — MSFT 525P 10/30 · 1 contract · "
        "Entry $4.75 @ 9:13 CT → Exit $6.65 @ 9:47 CT · +40.0% / +$190 · "
        "Target +25% hit · Today 1W/0L · +$190"
    )
    assert "Sure Winner" not in text
    assert "SURE WINNER" not in text


def test_gold_setup_copy_does_not_say_sure_winner():
    assert plain_exit_reason("stop loss -67% ≤ −50%", tier="gold") == "Stop −50% hit"
    assert plain_exit_reason("flatten 10:30 CT", tier="sniper") == "10:30 CT flat"
    assert plain_exit_reason("trail gave back half the peak gain") == "Trail"
    assert plain_exit_reason("scale out +30% — sell 1 of 2") == "Scale-out"
    text = format_exit_alert(
        tier="gold",
        symbol="WFC",
        strike=82,
        right="P",
        expiry="2026-10-30",
        contracts=1,
        entry=1.20,
        entry_at=datetime(2026, 10, 30, 9, 5, tzinfo=CT),
        exit_price=0.60,
        exit_at=datetime(2026, 10, 30, 10, 2, tzinfo=CT),
        reason="stop loss -50% ≤ −50%",
        tally="Today 0W/1L · −$60",
    )
    assert text.startswith("⭐ GOLD SETUP EXIT — WFC 82P 10/30")
    assert "Stop −50% hit" in text
    assert "Sure Winner" not in text


def test_exit_alert_is_deduped_by_order_id(tmp_path: Path):
    now = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    sent: list[str] = []
    path = tmp_path / "seen.json"
    first = dispatch_exit_alerts(
        [_intent()],
        now=now,
        seen_path=path,
        send=lambda message: sent.append(message) or {"ok": True},
    )
    second = dispatch_exit_alerts(
        [_intent(fill_price=7.0, updated_at="2026-10-30T15:10:00+00:00")],
        now=now,
        seen_path=path,
        send=lambda message: sent.append(message) or {"ok": True},
    )
    assert len(first) == 1
    assert second == []
    assert len(sent) == 1
    assert "Today 1W/0L · +$190" in sent[0]


def test_pending_fill_after_two_minutes_and_not_before(tmp_path: Path):
    staged = "2026-10-30T14:00:00+00:00"
    order = _intent(
        status="submitted",
        fill_price=None,
        limit_price=5.94,
        reason="take profit +25% ≥ +25%",
        created_at=staged,
        updated_at=staged,
        meta={"exit_clock": staged, "entry_price": 4.75, "entry_at": staged, "exit_tier": "sniper"},
    )
    early = datetime(2026, 10, 30, 14, 1, 30, tzinfo=timezone.utc)
    assert collect_exit_alerts([order], now=early, seen=set()) == []
    due = datetime(2026, 10, 30, 14, 2, 5, tzinfo=timezone.utc)
    alerts = collect_exit_alerts([order], now=due, seen=set())
    assert len(alerts) == 1
    assert "pending fill" in alerts[0]["message"]
    assert "Target +25% hit" in alerts[0]["message"]
    path = tmp_path / "seen.json"
    sent: list[str] = []
    dispatch_exit_alerts([order], now=due, seen_path=path, send=lambda message: sent.append(message) or {"ok": True})
    filled = _intent(
        status="filled",
        fill_price=5.94,
        meta={"exit_clock": staged, "entry_price": 4.75, "entry_at": staged, "exit_tier": "sniper"},
    )
    again = dispatch_exit_alerts(
        [filled],
        now=due + timedelta(minutes=5),
        seen_path=path,
        send=lambda message: sent.append(message) or {"ok": True},
    )
    assert again == []
    assert len(sent) == 1


def test_tally_counts_the_tier_for_the_day():
    now = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)
    winner = _intent(id="a", fill_price=6.65)
    loser = _intent(
        id="b",
        symbol="MU",
        strike=950,
        contract="MU261030P00950000",
        fill_price=2.00,
        updated_at="2026-10-30T16:00:00+00:00",
        reason="stop loss -58% ≤ −40%",
        meta={"entry_price": 4.75, "exit_tier": "sniper", "entry_at": "2026-10-30T14:13:00+00:00"},
    )
    alerts = {row["order_id"]: row for row in collect_exit_alerts([winner, loser], now=now, seen=set())}
    assert "Today 1W/1L" in alerts["b"]["message"]
    assert "−$" in alerts["b"]["message"]
    assert "Sure Winner" not in alerts["b"]["message"]


def test_flatten_does_not_use_the_pending_fallback():
    now = datetime(2026, 10, 30, 16, 0, tzinfo=timezone.utc)
    order = _intent(
        status="submitted",
        fill_price=None,
        reason="flatten 10:30 CT",
        meta={"exit_tier": "diamond", "entry_price": 1.5, "exit_clock": "2026-10-30T14:00:00+00:00"},
    )
    assert collect_exit_alerts([order], now=now, seen=set()) == []


def test_spx_exit_uses_the_modeled_result_and_is_deduped(tmp_path: Path):
    sent: list[str] = []
    board = {
        "spx_exits": [
            {
                "symbol": "SPX",
                "tier": "sniper",
                "reason": "target",
                "option_pct": 25,
                "ask": 4.00,
                "at_ct": "09:05",
                "exit_at_ct": "09:20",
                "day": "2026-10-30",
                "right": "C",
                "strike": 5800,
                "expiry": "2026-10-30",
                "order_id": "spx-sniper-2026-10-30",
                "contracts": 1,
            }
        ]
    }
    path = tmp_path / "seen.json"
    now = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    first = dispatch_exit_alerts(
        [],
        now=now,
        seen_path=path,
        sniper=board,
        send=lambda message: sent.append(message) or {"ok": True},
    )
    second = dispatch_exit_alerts(
        [],
        now=now,
        seen_path=path,
        sniper=board,
        send=lambda message: sent.append(message) or {"ok": True},
    )
    assert len(first) == 1 and second == []
    assert sent[0].startswith("🎯 SNIPER EXIT — SPX 5800C 10/30 · 1 contract")
    assert "Target +25% hit" in sent[0]
    assert "Sure Winner" not in sent[0]


def _arm_sniper(broker: WebullBroker, *, symbol: str, strike: float, expiry: str, fill: float) -> None:
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    intent = broker.stage(
        desk="sniper",
        action="BUY",
        symbol=symbol,
        right="P",
        contract=f"{symbol}{yymmdd}P{int(round(strike * 1000)):08d}",
        expiry=expiry,
        strike=strike,
        quantity=1,
        limit_price=fill,
        reason="sniper fixture",
        meta={"golden_kind": "sniper", "original_qty": 1, "gate": "passed"},
    )
    intent.status = "filled"
    intent.fill_price = fill
    broker.save()


def test_sniper_flatten_retries_until_flat_and_ignores_the_buy_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(WebullBroker, "list_option_positions", lambda self, account_id=None: None)
    broker = WebullBroker(
        enabled=True,
        dry_run=True,
        ledger_path=tmp_path / "wb.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=1, max_golden_buys_per_sync=2)
    morning = session_et(11, 31)  # 10:31 CT
    expiry = iso_day(session_et(11, 0, weekday_offset=3))
    _arm_sniper(broker, symbol="MU", strike=950, expiry=expiry, fill=1.25)
    card = {
        "symbol": "QQQ",
        "right": "C",
        "confidence_tier": "HIGH",
        "sure_winner": True,
        "desk": "Sure Winner",
        "ask": 1.1,
        "bid": 1.0,
        "expiry": expiry,
        "strike": 480,
        "contract": f"QQQ{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}C00480000",
    }
    first = trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": [card]}, now=morning)
    sells = [row for row in first["submitted"] if row["action"] == "SELL"]
    assert len(sells) == 1
    assert "10:30" in sells[0]["reason"]
    assert sells[0]["symbol"] == "MU"
    # The golden buy used the only budget slot. The flatten still went out.
    assert any(row["action"] == "BUY" for row in first["submitted"])

    quiet = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning + timedelta(seconds=60),
    )
    assert [row for row in quiet["submitted"] if row["action"] == "SELL"] == []

    retry = trader.sync(
        actions={"buy_now": [], "sell_now": []},
        sure_winners={"winners": []},
        now=morning + timedelta(minutes=3),
    )
    resent = [row for row in retry["submitted"] if row["action"] == "SELL"]
    assert len(resent) == 1
    assert any(order.status == "cancelled" and order.action == "SELL" for order in broker.orders)
    assert "10:30" in resent[0]["reason"]


def test_gold_stop_uses_the_fill_and_does_not_chase_a_gap(tmp_path: Path, monkeypatch):
    morning = session_et(11, 0)
    expiry = iso_day(session_et(11, 0, weekday_offset=4))
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        ledger_path=tmp_path / "live.json",
        account_id="margin-1",
        submit_interval_sec=0,
    )
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=4)
    yymmdd = f"{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}"
    contract = f"WFC{yymmdd}P00082000"
    buy = broker.stage(
        desk="sure_winner",
        action="BUY",
        symbol="WFC",
        right="P",
        contract=contract,
        expiry=expiry,
        strike=82,
        quantity=1,
        limit_price=3.0,
        reason="gold fixture",
        meta={"golden_kind": "sure_winner_gold", "original_qty": 1, "gate": "passed"},
    )
    buy.status = "filled"
    buy.fill_price = 3.0
    broker.save()
    pos = {
        "contract": contract,
        "symbol": "WFC",
        "right": "P",
        "expiry": expiry,
        "strike": 82,
        "quantity": 1,
        "cost": 1.98,
        "mark": 1.40,
    }
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [] if account_id == "cash-2" else [pos])
    monkeypatch.setattr(broker, "_fetch_order_detail", lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0"})
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: None)
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: "SELL-WFC")
    sold = trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=morning)
    sells = [row for row in sold["submitted"] if row["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["limit_price"] == 1.50
    assert "−50%" in sells[0]["reason"]
    live = next(order for order in broker.orders if order.action == "SELL")
    live.status = "submitted"
    pos["mark"] = 0.99
    again = trader.sync(actions={"buy_now": [], "sell_now": []}, sure_winners={"winners": []}, now=morning + timedelta(minutes=5))
    assert [row for row in again["submitted"] if row["action"] == "SELL"] == []
    assert live.status == "submitted"
    assert live.limit_price == 1.50


def test_ledger_keeps_the_buy_a_later_sell_still_needs(tmp_path: Path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", account_id="margin-1")
    first = None
    for index in range(501):
        intent = broker.stage(
            desk="sure_winner",
            action="BUY",
            symbol="QQQ",
            right="C",
            contract=f"QQQ261030C{index:08d}",
            expiry="2026-10-30",
            strike=500 + index,
            quantity=1,
            limit_price=1.0,
            reason="pad",
            meta={"golden_kind": "sure_winner_gold"},
        )
        intent.status = "filled"
        intent.fill_price = 1.0
        if first is None:
            first = intent
    assert first is not None
    # The first buy is the oldest. A newest-500 cap would drop it before the sell.
    sell = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="QQQ",
        right="C",
        contract=first.contract,
        expiry="2026-10-30",
        strike=first.strike,
        quantity=1,
        limit_price=1.2,
        reason="target",
        meta={"exit_tier": "gold", "entry_price": 1.0, "closes_order_id": first.id},
    )
    sell.status = "filled"
    sell.fill_price = 1.2
    broker.save()
    reloaded = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", account_id="margin-1")
    buys = [order for order in reloaded.orders if order.action == "BUY" and order.contract == first.contract]
    assert len(buys) == 1
    assert buys[0].fill_price == 1.0
    kept = orders_for_ledger(reloaded.orders, limit=500)
    assert any(order.id == first.id for order in kept)
