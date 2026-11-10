"""Expired lots close at intrinsic value and count in P&L."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401
from odte_scanner.edge import round_trips as rt
from odte_scanner.scorecard.daily_pnl import build_scorecard_daily_pnl
from odte_scanner.trading import expiry
from odte_scanner.trading.portal_book import reconcile_pnl, round_trips, status_label
from odte_scanner.trading.webull import WebullBroker, flatten_history_group

AFTER = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def _broker(tmp_path: Path) -> WebullBroker:
    return WebullBroker(enabled=True, dry_run=True, ledger_path=tmp_path / "wb.json", submit_interval_sec=0)


def _buy(broker, symbol, strike, right, price, qty=1, expiry_day="2026-11-04", kind="sniper"):
    contract = f"{symbol}{expiry_day[2:4]}{expiry_day[5:7]}{expiry_day[8:10]}{right}{int(strike * 1000):08d}"
    row = broker.stage(desk="golden", action="BUY", symbol=symbol, right=right, contract=contract,
                       expiry=expiry_day, strike=strike, quantity=qty, limit_price=price,
                       meta={"golden_kind": kind})
    row.status = "filled"
    row.fill_price = price
    row.meta["filled_qty"] = qty
    row.created_at = f"{expiry_day}T14:00:00+00:00"
    broker.save()
    return row


def test_otm_and_itm_expiries_close_at_intrinsic(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rt, "PATH", tmp_path / "trips.jsonl")
    broker = _broker(tmp_path)
    _buy(broker, "AAPL", 350, "C", 2.0)            # close 345 -> worthless
    _buy(broker, "TSLA", 345, "P", 3.0, qty=2)      # close 340 -> $5 ITM
    closes = {"AAPL": 345.0, "TSLA": 340.0}
    out = expiry.close_expired(broker, AFTER, close_fn=lambda s, d: closes[s])
    assert out["closed"] == 2 and out["itm"] == 1
    rows = [o.to_dict() if hasattr(o, "to_dict") else o.__dict__ for o in broker.orders]
    trips = {t["symbol"]: t for t in round_trips(rows)}
    assert trips["AAPL"]["pnl"] == -200.0 and trips["AAPL"]["expired"] is True
    assert trips["TSLA"]["pnl"] == 400.0 and trips["TSLA"]["itm"] is True
    assert reconcile_pnl(rows)["match"] is True
    sell = next(o for o in broker.orders if o.action == "SELL" and o.symbol == "TSLA")
    assert "ITM" in sell.meta["flags"][0]
    assert status_label(sell.__dict__) == "expired ITM"
    assert sell.broker_order_id is None and sell.desk == expiry.EXPIRY_DESK
    # idempotent
    assert expiry.close_expired(broker, AFTER, close_fn=lambda s, d: closes[s])["closed"] == 0
    # retained round trips include the zero exit
    assert {r["contract"][:4] for r in rt.load_round_trips(tmp_path / "trips.jsonl")} == {"AAPL", "TSLA"}


def test_sold_lots_and_unsettled_expiries_are_left_alone(tmp_path: Path):
    broker = _broker(tmp_path)
    b = _buy(broker, "AMZN", 262.5, "C", 1.5)
    sell = broker.stage(desk="golden_exit", action="SELL", symbol="AMZN", contract=b.contract,
                        expiry=b.expiry, strike=262.5, limit_price=1.77)
    sell.status = "filled"
    sell.fill_price = 1.77
    _buy(broker, "SPY", 746, "C", 2.76, expiry_day="2026-11-09")   # same day, before 4:20 PM ET
    _buy(broker, "SPX", 6000, "C", 4.0)                             # never ours to close
    out = expiry.close_expired(broker, AFTER, close_fn=lambda s, d: 999.0)
    assert out["closed"] == 0


def test_missing_close_waits_and_daily_pnl_includes_the_expiry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rt, "PATH", tmp_path / "trips.jsonl")
    broker = _broker(tmp_path)
    _buy(broker, "IWM", 279, "C", 0.92)
    assert expiry.close_expired(broker, AFTER, close_fn=lambda s, d: None)["waiting_for_close"] == 1
    assert expiry.close_expired(broker, AFTER, close_fn=lambda s, d: 275.0)["closed"] == 1
    rows = [o.__dict__ for o in broker.orders]
    board = build_scorecard_daily_pnl(rows, now=AFTER, load_side=False, retained=[])
    text = repr(board)
    assert "-92" in text


def test_real_webull_sell_voids_the_estimate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rt, "PATH", tmp_path / "trips.jsonl")
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id="a",
                          ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    b = _buy(broker, "AAPL", 350, "C", 2.0)
    expiry.close_expired(broker, AFTER, close_fn=lambda s, d: 345.0)
    group = {
        "client_order_id": "wb-realsell01", "order_id": "X1", "status": "FILLED",
        "orders": [{"symbol": "AAPL", "side": "SELL", "status": "FILLED", "filled_price": "2.33",
                    "filled_quantity": "1", "total_quantity": "1", "limit_price": "2.28",
                    "place_time_at": "2026-11-04T15:00:00Z", "filled_time_at": "2026-11-04T15:00:02Z",
                    "position_intent": "SELL_TO_CLOSE",
                    "legs": [{"symbol": "AAPL", "option_type": "CALL", "strike_price": "350",
                              "option_expire_date": "2026-11-04", "side": "SELL"}]}],
    }
    summary = broker.import_history(flatten_history_group(group))
    assert summary.get("voided_expiry") == 1
    est = next(o for o in broker.orders if o.meta.get("expiry_close"))
    assert est.status == "cancelled"
    rows = [o.__dict__ for o in broker.orders]
    trips = [t for t in round_trips(rows) if t["contract"] == b.contract]
    assert len(trips) == 1 and trips[0]["exit"] == 2.33
    assert expiry.close_expired(broker, AFTER, close_fn=lambda s, d: 345.0)["closed"] == 0
