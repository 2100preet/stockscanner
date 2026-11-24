"""Afternoon Puts paper execution: stop watch, stale lots, and closes after flat.

Nov 6: the QQQ −40% stop never went out. Nov 19: closes for expired Nov 17/18
lots. Nov 20: closes after our own sell had filled ("close intent mismatch").
All of this is scoped to Afternoon Puts lots. Sniper and the live broker keep
their behavior.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import odte_scanner.edge  # noqa: F401  (import order)
import odte_scanner.trading.auto_trader as at
from odte_scanner.trading.afternoon_lots import is_flat_reject, ledger_open_qty
from odte_scanner.trading.auto_trader import AutoTrader
from odte_scanner.trading.webull import WebullBroker
from tests.session_clock import iso_day, session_et

MISMATCH = ("HTTP Status: 417, Code: OPENAPI_POSITION_ORDER_INTENT_MISMATCH, Msg: close intent mismatches "
            "position direction., RequestID: r1")


def _occ(symbol, expiry, right, strike):
    return f"{symbol}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{right}{int(round(strike * 1000)):08d}"


def _desk(tmp_path: Path, monkeypatch, *, desk="afternoon_puts", fill=2.00, days_ago=0, error=None):
    """One filled lot. Positions, quotes and Webull replies are all fakes."""
    entry_at = session_et(12, 46) - timedelta(days=days_ago)  # 11:46 CT
    expiry = iso_day(entry_at)
    broker = WebullBroker(enabled=True, dry_run=False, app_key="k", app_secret="s", account_id="a",
                          ledger_path=tmp_path / "wb.json", submit_interval_sec=0)
    broker._hold_outside_session = lambda intent: None  # type: ignore[method-assign]
    broker._fetch_order_detail = lambda intent: None  # type: ignore[method-assign]
    broker._cancel_via_openapi = lambda intent: None  # type: ignore[method-assign]
    right = "P" if desk == "afternoon_puts" else "C"
    contract = _occ("QQQ", expiry, right, 655)
    meta = ({"golden_kind": "afternoon_puts", "gold_exit": True, "afternoon_flat_ct": "14:45"}
            if desk == "afternoon_puts" else {"golden_kind": "sniper", "gold_exit": True, "sniper_flat_ct": "10:30"})
    buy = broker.stage(desk=desk, action="BUY", symbol="QQQ", right=right, contract=contract, expiry=expiry,
                       strike=655, limit_price=fill, meta=meta)
    buy.status, buy.fill_price, buy.broker_order_id = "filled", fill, "BQQQ"
    buy.created_at = entry_at.isoformat()
    broker.save()
    book = {"rows": None}  # None = positions call failed; [] = account shows no such lot
    monkeypatch.setattr(broker, "list_option_positions",
                        lambda account_id=None: None if book["rows"] is None else [dict(p) for p in book["rows"]])
    sent: list[tuple[str, str, str, float]] = []

    def place(intent):
        sent.append((intent.action, intent.symbol, intent.reason, float(intent.limit_price or 0)))
        if error and intent.action == "SELL":
            raise RuntimeError(error)
        return "X" + intent.id

    broker._place_via_openapi = place  # type: ignore[method-assign]
    monkeypatch.setattr(at, "_maybe_import_history", lambda *a, **k: None)
    quotes = {"bid": None, "calls": 0}

    def fake_quote(order):
        quotes["calls"] += 1
        if quotes["bid"] is None:
            return None, "no_quote"
        return quotes["bid"], "tradier_bid"

    monkeypatch.setattr(at, "quote_mark", fake_quote)
    trader = AutoTrader(broker, golden_only=True, max_orders_per_sync=3)
    pos = {"contract": contract, "symbol": "QQQ", "right": right, "expiry": expiry, "strike": 655,
           "quantity": 1, "cost": fill}
    return broker, trader, buy, book, pos, quotes, sent


def _sells(sent):
    return [s for s in sent if s[0] == "SELL"]


def _filled_sell(broker, buy, *, fill, minutes_after=60, reason="trail gave back half the peak gain — sell 1"):
    sell = broker.stage(desk="golden_exit", action="SELL", symbol=buy.symbol, right=buy.right, contract=buy.contract,
                        expiry=buy.expiry, strike=buy.strike, limit_price=fill, reason=reason,
                        meta={"closes_order_id": buy.id, "exit_tier": "afternoon", "filled_qty": 1.0})
    sell.status, sell.fill_price = "filled", fill
    sell.created_at = (at._parse_iso(buy.created_at) + timedelta(minutes=minutes_after)).isoformat()
    broker.save()
    return sell


# --- Bug a: the stop is watched even when Webull's snapshot drops the lot ---

def test_stop_fires_when_lot_is_missing_from_the_positions_snapshot(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    book["rows"] = []  # Nov 6: the snapshot came back without the QQQ lot
    quotes["bid"] = 1.10  # −45% on a $2.00 fill
    trader.sync_exits(now=session_et(13, 37))  # 12:37 CT
    sells = _sells(sent)
    assert len(sells) == 1 and "stop" in sells[0][2]
    assert buy.meta["exit_mark_source"] == "tradier_bid" and buy.meta["exit_mark"] == 1.10


def test_stop_fires_when_the_positions_call_fails(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    book["rows"] = None
    quotes["bid"] = 1.15
    trader.sync_exits(now=session_et(13, 37))
    assert any("stop" in s[2] for s in _sells(sent))


def test_webull_mark_still_wins_when_the_lot_is_listed(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    book["rows"] = [dict(pos, bid=1.10, mark=1.10)]
    quotes["bid"] = 5.00  # would not be a stop; must not be read
    trader.sync_exits(now=session_et(13, 37))
    assert any("stop" in s[2] for s in _sells(sent))
    assert quotes["calls"] == 0


def test_no_quote_and_no_mark_sends_nothing_before_the_clock(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    book["rows"] = []
    trader.sync_exits(now=session_et(13, 37))
    assert _sells(sent) == []
    assert buy.meta["exit_mark_source"] == "no_quote"


def test_open_lot_missing_from_snapshot_still_flattens_at_245(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    book["rows"] = []
    trader.sync_exits(now=(session_et(15, 45) + timedelta(seconds=5)))  # 2:45:05 PM CT
    sells = _sells(sent)
    assert len(sells) == 1 and sells[0][2] == "flatten 2:45 PM CT"


# --- Bug c: no close once our own sell has filled ---

def test_no_flatten_after_our_sell_filled_when_positions_fail(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, fill=4.15)
    _filled_sell(broker, buy, fill=8.30)  # Nov 20 QQQ 625P trail at 1:44 PM CT
    book["rows"] = None  # positions call failed at 2:45
    for step in range(4):
        trader.sync_exits(now=(session_et(15, 45) + timedelta(seconds=5)) + timedelta(seconds=40 * step))
    assert _sells(sent) == []
    assert ledger_open_qty(buy, broker.orders) == 0


def test_snapshot_that_still_lists_a_sold_lot_does_not_resell(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, fill=4.15)
    _filled_sell(broker, buy, fill=8.30)
    book["rows"] = [dict(pos, bid=3.0, mark=3.0)]  # a stale snapshot row
    trader.sync_exits(now=(session_et(15, 45) + timedelta(seconds=5)))
    assert _sells(sent) == []


def test_close_intent_mismatch_marks_the_lot_gone_and_stops_retries(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, error=MISMATCH)
    book["rows"] = None  # the recheck can't see the book either
    trader.sync_exits(now=(session_et(15, 45) + timedelta(seconds=5)))
    assert len(_sells(sent)) == 1
    sell = next(o for o in broker.orders if o.action == "SELL")
    assert sell.status == "rejected" and sell.meta["reject_kind"] == "no_position"
    assert buy.meta["position_gone"] is True
    assert "OPENAPI_POSITION_ORDER_INTENT_MISMATCH" in buy.meta["position_gone_reason"]
    for step in range(1, 6):
        trader.sync_exits(now=(session_et(15, 45) + timedelta(seconds=5)) + timedelta(seconds=60 * step))
    assert len(_sells(sent)) == 1


def test_flat_reject_matcher():
    assert is_flat_reject("OPENAPI_POSITION_ORDER_INTENT_MISMATCH", "")
    assert is_flat_reject(None, "Parameter error, invalid market,symbol,option_type")
    assert not is_flat_reject("OPENAPI_SOMETHING_ELSE", "Order failed.")


# --- Bug b / Nov 19: expired prior-day lots never flatten or block ---

def test_expired_prior_day_lot_is_not_flattened(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, days_ago=1)
    book["rows"] = None  # Nov 19: positions call failed at 2:45 PM CT
    trader.sync_exits(now=session_et(15, 47))
    assert _sells(sent) == []
    assert trader._held_qty(buy) == 0


def test_expired_lot_does_not_block_a_new_entry(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, days_ago=1)
    trader._now = session_et(12, 50)
    assert trader._already_open("QQQ", "afternoon_puts", "P", None) is False
    # Same-day lots still block, so it stays one entry per name a day.
    today = session_et(12, 50)
    fresh = broker.stage(desk="afternoon_puts", action="BUY", symbol="QQQ", right="P",
                         contract=_occ("QQQ", iso_day(today), "P", 650), expiry=iso_day(today), strike=650,
                         limit_price=1.5, meta={"golden_kind": "afternoon_puts"})
    fresh.status, fresh.fill_price = "filled", 1.5
    assert trader._already_open("QQQ", "afternoon_puts", "P", None) is True


def test_stale_lot_rule_leaves_other_desks_alone(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, desk="sniper", days_ago=1)
    trader._now = session_et(10, 0)
    assert trader._already_open("QQQ", "sniper", "C", None) is True  # unchanged behavior


# --- Scope: Sniper and the live broker are unchanged ---

def test_sniper_lot_missing_from_snapshot_keeps_old_behavior(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch, desk="sniper")
    book["rows"] = []
    quotes["bid"] = 0.50
    trader.sync_exits(now=session_et(10, 15))
    assert _sells(sent) == [] and quotes["calls"] == 0
    assert trader._held_qty(buy) == 0


def test_live_broker_never_uses_the_afternoon_rules(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    monkeypatch.setattr(trader, "_is_live_desk_broker", lambda: True)
    book["rows"] = []
    quotes["bid"] = 1.10
    trader.sync_exits(now=session_et(13, 37))
    assert _sells(sent) == [] and quotes["calls"] == 0


def test_partial_sell_only_counts_the_filled_part(tmp_path: Path, monkeypatch):
    broker, trader, buy, book, pos, quotes, sent = _desk(tmp_path, monkeypatch)
    buy.quantity = 2
    buy.meta["filled_qty"] = 2.0
    sell = _filled_sell(broker, buy, fill=4.0)
    sell.status = "partial"
    sell.meta["filled_qty"] = 1.0
    assert ledger_open_qty(buy, broker.orders) == 1.0
    sell.meta["filled_qty"] = 0.0
    assert ledger_open_qty(buy, broker.orders) == 2.0
