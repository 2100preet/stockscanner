"""Ledger integrity, stuck staged rows, and Webull-history reconcile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker, flatten_history_group
from tests.session_clock import iso_day, session_et


def _live_broker(path: Path) -> WebullBroker:
    broker = WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        account_id="acct-1",
        ledger_path=path,
        submit_interval_sec=0,
    )
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    return broker


def _occ(symbol: str, expiry: str, right: str, strike: float) -> str:
    return f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{right}{int(round(strike * 1000)):08d}"


def test_second_broker_save_does_not_drop_a_sell_the_first_wrote(tmp_path: Path):
    path = tmp_path / "wb.json"
    first = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    buy = first.stage(desk="sniper", action="BUY", symbol="SPY", contract="SPY261109C00746000",
                      expiry="2026-11-09", strike=746, limit_price=2.76)
    buy.status = "filled"
    first.save()
    stale = WebullBroker(enabled=False, dry_run=True, ledger_path=path)  # loads now
    sell = first.stage(desk="golden_exit", action="SELL", symbol="SPY", contract="SPY261109C00746000",
                       expiry="2026-11-09", strike=746, limit_price=9.2)
    sell.status = "filled"
    first.save()
    # The stale broker edits the buy (peak mark) and saves.
    stale.orders[0].meta["peak_mark"] = 9.2
    stale.save()
    again = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    by_id = {o.id: o for o in again.orders}
    assert sell.id in by_id and by_id[sell.id].status == "filled"
    assert by_id[buy.id].meta.get("peak_mark") == 9.2


def test_stale_staged_copy_never_overwrites_a_sent_row(tmp_path: Path):
    path = tmp_path / "wb.json"
    first = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    row = first.stage(desk="golden_exit", action="SELL", symbol="AAPL", contract="AAPL261104C00350000",
                      expiry="2026-11-04", strike=350, limit_price=2.28)
    stale = WebullBroker(enabled=False, dry_run=True, ledger_path=path)  # sees "staged"
    row.status = "submitted"
    row.broker_order_id = "B1"
    first.save()
    stale.orders[0].meta["note"] = "touched"
    stale.orders[0].updated_at = "2099-01-01T00:00:00+00:00"
    stale.save()
    again = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    assert again.orders[0].status == "submitted"
    assert again.orders[0].broker_order_id == "B1"


def test_reconcile_staged_adopts_resends_and_clears(tmp_path: Path):
    broker = _live_broker(tmp_path / "wb.json")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=5)).isoformat()
    young = (now - timedelta(seconds=40)).isoformat()
    known = broker.stage(desk="golden_exit", action="SELL", symbol="AAPL", contract="AAPL261104C00350000",
                         expiry="2026-11-04", strike=350, limit_price=2.28)
    lost_buy = broker.stage(desk="sniper", action="BUY", symbol="IWM", contract="IWM261109C00279000",
                            expiry="2026-11-09", strike=279, limit_price=0.92)
    young_buy = broker.stage(desk="sniper", action="BUY", symbol="SPY", contract="SPY261109C00747000",
                             expiry="2026-11-09", strike=747, limit_price=2.5)
    lost_sell = broker.stage(desk="golden_exit", action="SELL", symbol="TSLA", contract="TSLA261106P00345000",
                             expiry="2026-11-06", strike=345, right="P", limit_price=0.2)
    fresh = broker.stage(desk="sniper", action="BUY", symbol="QQQ", contract="QQQ261109C00700000",
                         expiry="2026-11-09", strike=700, limit_price=1.0)
    known.created_at = old
    lost_buy.created_at = old
    lost_sell.created_at = old
    young_buy.created_at = young
    fresh.created_at = now.isoformat()

    def detail(intent):
        if intent.id == known.id:
            return {"order_id": "WB-9", "status": "FILLED", "filled_quantity": "1", "total_quantity": "1",
                    "filled_price": "2.33"}
        raise RuntimeError("HTTP Status: 417, Code: OPENAPI_PARAM_ERR, Msg: Parameter error, Order not present.")

    sent: list[str] = []
    broker._fetch_order_detail = detail  # type: ignore[method-assign]
    broker._place_via_openapi = lambda intent: sent.append(intent.id) or "WB-NEW"  # type: ignore[method-assign]
    actions = {a["id"]: a["action"] for a in broker.reconcile_staged(now)}
    assert actions[known.id] == "adopted"
    assert known.status == "filled" and known.fill_price == 2.33 and known.broker_order_id == "WB-9"
    assert actions[young_buy.id] == "resent" and young_buy.status == "submitted"
    assert sent == [young_buy.id]
    assert actions[lost_buy.id] == "cleared" and lost_buy.status == "cancelled"
    assert lost_buy.meta["never_sent"] is True
    assert actions[lost_sell.id] == "cleared" and lost_sell.status == "cancelled"
    assert fresh.id not in actions and fresh.status == "staged"


def test_lookup_failure_leaves_the_row_for_next_cycle(tmp_path: Path):
    broker = _live_broker(tmp_path / "wb.json")
    now = datetime.now(timezone.utc)
    row = broker.stage(desk="golden_exit", action="SELL", symbol="AAPL", contract="AAPL261104C00350000",
                       expiry="2026-11-04", strike=350, limit_price=2.28)
    row.created_at = (now - timedelta(minutes=2)).isoformat()

    def boom(intent):
        raise RuntimeError("connection reset")

    broker._fetch_order_detail = boom  # type: ignore[method-assign]
    assert broker.reconcile_staged(now) == []
    assert row.status == "staged"


def test_staged_buy_is_not_flattened_but_filled_lots_are(tmp_path: Path, monkeypatch):
    """Nov 9: the unsent IWM 279C drew rejects while the three fills had no exit."""
    now = session_et(11, 31)  # 10:31 CT, past the Sniper flatten
    expiry = iso_day(now)
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", account_id="acct-1")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    filled = []
    for sym, strike, cost in (("SPY", 746, 2.76), ("IWM", 278, 1.34), ("SPY", 747, 2.57)):
        b = broker.stage(desk="sniper", action="BUY", symbol=sym, contract=_occ(sym, expiry, "C", strike),
                         expiry=expiry, strike=strike, limit_price=cost,
                         meta={"golden_kind": "sniper", "gold_exit": True})
        b.status = "filled"
        b.fill_price = cost
        b.created_at = (now - timedelta(minutes=100)).isoformat()
        filled.append(b)
    stuck = broker.stage(desk="sniper", action="BUY", symbol="IWM", contract=_occ("IWM", expiry, "C", 279),
                         expiry=expiry, strike=279, limit_price=0.92, meta={"golden_kind": "sniper"})
    stuck.created_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    broker.save()
    book = [
        {"contract": b.contract, "symbol": b.symbol, "right": "C", "expiry": expiry, "strike": b.strike,
         "quantity": 1, "cost": b.fill_price, "mark": b.fill_price * 1.1}
        for b in filled
    ]
    monkeypatch.setattr(broker, "list_option_positions", lambda account_id=None: [dict(p) for p in book])
    out = trader.sync(now=now)
    sold = sorted(r["contract"] for r in out["submitted"] if r["action"] == "SELL")
    assert sold == sorted(b.contract for b in filled)
    assert stuck.contract not in sold
    assert stuck.status == "cancelled"  # cleared by the reconciler (bridge is dry-run)


def test_stale_staged_sell_does_not_block_the_next_exit(tmp_path: Path, monkeypatch):
    now = session_et(10, 31)
    expiry = iso_day(now)
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", account_id="acct-1")
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    contract = _occ("SPY", expiry, "C", 746)
    buy = broker.stage(desk="sniper", action="BUY", symbol="SPY", contract=contract, expiry=expiry,
                       strike=746, limit_price=2.76, meta={"golden_kind": "sniper"})
    buy.status = "filled"
    buy.fill_price = 2.76
    stuck = broker.stage(desk="golden_exit", action="SELL", symbol="SPY", contract=contract, expiry=expiry,
                         strike=746, limit_price=2.0)
    stuck.created_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    broker.save()
    trader._now = now
    assert trader._sell_already_working({"symbol": "SPY", "contract": contract}, "C") is False


def test_history_import_restores_lost_sells_and_fixes_staged(tmp_path: Path):
    broker = _live_broker(tmp_path / "wb.json")
    buy = broker.stage(desk="sniper", action="BUY", symbol="SPY", contract="SPY261109C00746000",
                       expiry="2026-11-09", strike=746, limit_price=2.76, meta={"golden_kind": "sniper"})
    buy.status = "filled"
    buy.fill_price = 2.76
    buy.created_at = "2026-11-09T14:50:19+00:00"
    staged_sell = broker.stage(desk="golden_exit", action="SELL", symbol="AAPL", contract="AAPL261104C00350000",
                               expiry="2026-11-04", strike=350, limit_price=2.28)
    broker.save()
    group_lost = {
        "client_order_id": "wb-1dd102cf97",
        "orders": [{
            "client_order_id": "wb-1dd102cf97", "order_id": "X1", "side": "SELL", "status": "FILLED",
            "legs": [{"symbol": "SPY", "option_type": "CALL", "option_expire_date": "2026-11-09",
                      "strike_price": "746.00", "quantity": "1"}],
            "total_quantity": "1", "filled_quantity": "1", "limit_price": "9.20", "filled_price": "9.22",
            "place_time_at": "2026-11-09T20:26:47.480Z", "filled_time_at": "2026-11-09T20:26:56.744Z",
            "position_intent": "SELL_TO_CLOSE",
        }],
    }
    group_staged = {
        "client_order_id": staged_sell.id,
        "orders": [{
            "client_order_id": staged_sell.id, "order_id": "X2", "side": "SELL", "status": "FILLED",
            "legs": [{"symbol": "AAPL", "option_type": "CALL", "option_expire_date": "2026-11-04",
                      "strike_price": "350.00"}],
            "total_quantity": "1", "filled_quantity": "1", "filled_price": "2.33",
            "place_time_at": "2026-11-04T16:30:33.159Z", "filled_time_at": "2026-11-04T16:30:33.395Z",
        }],
    }
    rows = flatten_history_group(group_lost) + flatten_history_group(group_staged)
    summary = broker.import_history(rows)
    assert summary["added"] == 1 and summary["updated"] == 1
    restored = next(o for o in broker.orders if o.id == "wb-1dd102cf97")
    assert restored.action == "SELL" and restored.status == "filled" and restored.fill_price == 9.22
    assert restored.contract == "SPY261109C00746000"
    assert restored.meta["closes_order_id"] == buy.id
    assert restored.meta["exit_tier"] == "sniper"
    assert staged_sell.status == "filled" and staged_sell.fill_price == 2.33
    assert staged_sell.broker_order_id == "X2"
    # Idempotent.
    assert broker.import_history(rows) == {"added": 0, "updated": 0, "checked": 2}
