"""Skeptical-review edge cases across the exit stack."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import odte_scanner.edge  # noqa: F401
import odte_scanner.trading.auto_trader as at
from odte_scanner.trading import expiry
from odte_scanner.trading import risk_controls as rc
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker, flatten_history_group
from tests.session_clock import iso_day, session_et

CT = ZoneInfo("America/Chicago")


def _occ(symbol, expiry_day, right, strike):
    return f"{symbol}{expiry_day[2:4]}{expiry_day[5:7]}{expiry_day[8:10]}{right}{int(round(strike * 1000)):08d}"


def _live(path: Path, positions, sent, account="acct-1") -> WebullBroker:
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id=account,
                          ledger_path=path, submit_interval_sec=0)
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    broker._fetch_order_detail = lambda intent: None  # type: ignore[method-assign]
    broker.list_option_positions = lambda account_id=None: [dict(p) for p in positions]  # type: ignore[method-assign]

    def place(intent):
        sent.append((intent.action, intent.contract, intent.id))
        return "X" + intent.id

    broker._place_via_openapi = place  # type: ignore[method-assign]
    return broker


def _lot(broker, symbol="SPY", strike=746.0, fill=2.76, kind="sniper"):
    day = iso_day(session_et(9, 50))
    contract = _occ(symbol, day, "C", strike)
    buy = broker.stage(desk=kind, action="BUY", symbol=symbol, contract=contract, expiry=day, strike=strike,
                       limit_price=fill, meta={"golden_kind": kind, "gold_exit": True, "sniper_flat_ct": "10:30"})
    buy.status, buy.fill_price, buy.broker_order_id = "filled", fill, "B1"
    buy.created_at = session_et(9, 50).isoformat()
    broker.save()
    return buy, {"contract": contract, "symbol": symbol, "right": "C", "expiry": day, "strike": strike,
                 "quantity": 1, "cost": fill, "bid": fill * 1.1, "mark": fill * 1.1}


def _sells(sent):
    return [s for s in sent if s[0] == "SELL"]


# --- double sells: two loops, two brokers, one ledger ------------------------


def test_exit_loop_and_scan_brokers_do_not_double_sell(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    path = tmp_path / "wb.json"
    sent: list = []
    scan_broker = _live(path, [], sent)
    _, pos = _lot(scan_broker)
    positions = [pos]
    # The exit loop's broker was loaded before the scan wrote anything else.
    exit_broker = _live(path, positions, sent)
    scan_broker.list_option_positions = lambda account_id=None: [dict(p) for p in positions]  # type: ignore[method-assign]
    scan = AutoTrader(scan_broker, golden_only=True, max_orders_per_sync=3)
    fast = AutoTrader(exit_broker, golden_only=True, max_orders_per_sync=3)
    flat = session_et(11, 30) + timedelta(seconds=5)
    scan.sync_exits(now=flat)
    fast.sync_exits(now=flat + timedelta(seconds=7))
    scan.sync_exits(now=flat + timedelta(seconds=14))
    assert len(_sells(sent)) == 1


def test_parallel_loops_serialize_on_the_sync_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    path = tmp_path / "wb.json"
    sent: list = []
    seed = _live(path, [], sent)
    _, pos = _lot(seed)
    traders = [AutoTrader(_live(path, [pos], sent), golden_only=True, max_orders_per_sync=3) for _ in range(4)]
    flat = session_et(11, 30) + timedelta(seconds=5)
    barrier = threading.Barrier(len(traders))

    def run(trader):
        barrier.wait()
        trader.sync_exits(now=flat, lock_timeout=10)

    threads = [threading.Thread(target=run, args=(t,)) for t in traders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(_sells(sent)) == 1


# --- restore-from-history desk -------------------------------------------------


def _restore(broker, cid, contract_strike=746.0, placed="2026-11-09T14:50:20.000Z", intent=None):
    order = {
        "client_order_id": cid, "order_id": "X" + cid, "side": "BUY", "status": "FILLED",
        "legs": [{"symbol": "SPY", "option_type": "CALL", "option_expire_date": "2026-11-14",
                  "strike_price": f"{contract_strike:.2f}"}],
        "total_quantity": "1", "filled_quantity": "1", "filled_price": "2.76",
        "place_time_at": placed, "filled_time_at": placed,
    }
    if intent:
        order["position_intent"] = intent
    broker.import_history(flatten_history_group({"client_order_id": cid, "orders": [order]}))
    return next(o for o in broker.orders if o.id == cid)


def _ledger_buy(broker, desk, created, account=None):
    meta = {"golden_kind": desk}
    if account:
        meta["account_id"] = account
    row = broker.stage(desk=desk, action="BUY", symbol="SPY", contract="SPY261114C00746000", expiry="2026-11-14",
                       strike=746, limit_price=2.7, meta=meta)
    row.status = "cancelled"
    row.created_at = created
    broker.save()
    return row


def test_restore_ignores_a_sibling_from_another_day(tmp_path):
    broker = _live(tmp_path / "wb.json", [], [])
    _ledger_buy(broker, "sure_winner", "2026-11-06T15:00:00+00:00")  # weekly, bought Friday by Gold
    restored = _restore(broker, "wb-r1")
    assert restored.desk == "webull_import" and "desk_from" not in restored.meta


def test_restore_prefers_the_chase_original_before_it(tmp_path):
    broker = _live(tmp_path / "wb.json", [], [])
    original = _ledger_buy(broker, "sniper", "2026-11-09T14:50:10+00:00")
    _ledger_buy(broker, "diamond", "2026-11-09T15:05:00+00:00")  # later, another desk, same weekly strike
    restored = _restore(broker, "wb-r2")
    assert restored.desk == "sniper" and restored.meta["desk_from"] == original.id


def test_restore_ignores_the_second_account(tmp_path):
    broker = _live(tmp_path / "wb.json", [], [])
    _ledger_buy(broker, "challenge", "2026-11-09T14:50:10+00:00", account="acct-2")
    restored = _restore(broker, "wb-r3")
    assert restored.desk == "webull_import"


def test_restored_buy_to_close_is_never_a_long(tmp_path):
    broker = _live(tmp_path / "wb.json", [], [])
    _ledger_buy(broker, "sniper", "2026-11-09T14:50:10+00:00")
    restored = _restore(broker, "wb-r4", intent="BUY_TO_CLOSE")
    assert restored.desk == "webull_import"
    assert AutoTrader._is_position_buy(restored) is False


# --- lunch wings -------------------------------------------------------------


def test_lunch_long_wing_position_is_never_sold_by_the_gold_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    day = iso_day(session_et(9, 50))
    long_c = _occ("SPY", day, "P", 740)
    short_c = _occ("SPY", day, "P", 742)
    positions = [{"contract": long_c, "symbol": "SPY", "right": "P", "expiry": day, "strike": 740,
                  "quantity": 1, "cost": 0.40, "bid": 0.10, "mark": 0.10}]  # -75%: would trip a stop
    sent: list = []
    broker = _live(tmp_path / "wb.json", positions, sent)
    combo = broker.stage(desk="lunch_credit", action="SELL", symbol="SPY", right="P", contract=short_c, expiry=day,
                         strike=742, limit_price=0.5, meta={"spread_id": "sp1", "long_contract": long_c,
                                                            "short_contract": short_c, "lunch_credit": True})
    combo.status = "filled"
    broker.save()
    AutoTrader(broker, golden_only=True, max_orders_per_sync=3).sync_exits(now=session_et(12, 30))
    assert _sells(sent) == []


# --- expiry day: never close a live 0DTE lot early ---------------------------


def test_expiry_close_never_runs_on_expiry_day_before_settlement(tmp_path):
    broker = WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    contract = _occ("SPY", "2026-11-10", "C", 746)
    buy = broker.stage(desk="sniper", action="BUY", symbol="SPY", contract=contract, expiry="2026-11-10",
                       strike=746, quantity=1, limit_price=2.0, meta={"golden_kind": "sniper"})
    buy.status, buy.fill_price = "filled", 2.0
    buy.created_at = "2026-11-10T14:50:00+00:00"
    broker.save()
    close = lambda s, d: 750.0  # noqa: E731
    for hh, mm in ((8, 30), (12, 0), (14, 59), (15, 0), (15, 19)):
        moment = datetime(2026, 11, 10, hh, mm, tzinfo=CT)
        assert expiry.close_expired(broker, moment, close_fn=close)["closed"] == 0, (hh, mm)
    assert expiry.close_expired(broker, datetime(2026, 11, 10, 15, 21, tzinfo=CT), close_fn=close)["closed"] == 1


def test_expiry_settlement_clock_in_daylight_time():
    # 2026-07-17 (CDT): 4:20 PM ET is 3:20 PM CT as well.
    assert expiry.is_settled("2026-07-17", datetime(2026, 7, 17, 15, 19, tzinfo=CT)) is False
    assert expiry.is_settled("2026-07-17", datetime(2026, 7, 17, 15, 20, tzinfo=CT)) is True
    assert expiry.is_settled("2026-07-17", datetime(2026, 7, 16, 23, 0, tzinfo=CT)) is False


# --- daily limits never block exits ------------------------------------------


def test_kill_switch_and_limits_never_block_exit_loop_sells(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    rc.set_kill_switch(True, "test")
    monkeypatch.setenv("PAPER_ENTRY_KILL", "1")
    sent: list = []
    broker = _live(tmp_path / "wb.json", [], sent)
    _, pos = _lot(broker, fill=2.50)
    pos["bid"] = pos["mark"] = 1.40  # -44%: stop
    broker.list_option_positions = lambda account_id=None: [dict(pos)]  # type: ignore[method-assign]
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    assert trader._entry_limit("sniper", {"symbol": "SPY"}) is not None  # entries are blocked
    trader.sync_exits(now=session_et(10, 15))
    assert len(_sells(sent)) == 1
