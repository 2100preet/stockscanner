"""Nov 17 exit bugs.

MRK 147C: the Gold stop rested past 4 PM ET, then 16 SELLs were staged after
the close. JPM 350C: eight cancel/replace rounds while the bid slid.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401  load first: signals.diamond <-> edge import cycle
from tests.session_clock import iso_day, session_et
from tests.test_gold_exits import _arm, _broker, _install, _pos, _sync, _trader


def _stop_book(tmp_path: Path, monkeypatch, *, at, rested_sec: float = 60, mark: float = 0.56):
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path, "live.json", live=True)
    trader = _trader(broker)
    pos = _pos("MRK", 147, weekly, cost=1.2, mark=mark, quantity=1, position_id="pos-mrk")
    buy = _arm(broker, pos)
    buy.created_at = (at - timedelta(hours=3)).astimezone(timezone.utc).isoformat()
    stop = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="MRK",
        right="C",
        contract=pos["contract"],
        expiry=weekly,
        strike=147,
        quantity=1,
        limit_price=0.60,
        reason="stop loss -53% ≤ −50%",
        meta={"gate": "exit", "hard_stop": True},
    )
    stop.status = "submitted"
    stop.broker_order_id = "WB-STOP"
    stop.created_at = (at - timedelta(seconds=rested_sec)).astimezone(timezone.utc).isoformat()
    stop.meta["exit_clock"] = stop.created_at
    broker.save()
    cancelled: list[str] = []
    placed: list = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "1"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-NEW")
    _install(monkeypatch, broker, lambda: [pos])
    return trader, broker, stop, cancelled, placed, pos, buy


def test_stop_above_the_bid_sells_at_the_bid_in_the_last_ten_minutes(tmp_path: Path, monkeypatch):
    at = session_et(15, 52)
    trader, _broker_, stop, cancelled, placed, _pos_, _buy = _stop_book(tmp_path, monkeypatch, at=at, rested_sec=60)
    _sync(trader, at)
    assert cancelled == [stop.id]
    assert stop.meta["hard_stop_timed_out"] is True
    assert len(placed) == 1 and placed[0].limit_price == 0.56


def test_stop_still_rests_earlier_in_the_day(tmp_path: Path, monkeypatch):
    at = session_et(14, 52)
    trader, _broker_, stop, cancelled, placed, _pos_, _buy = _stop_book(tmp_path, monkeypatch, at=at, rested_sec=60)
    _sync(trader, at)
    assert cancelled == [] and placed == []


def _deferred(broker, pos, *, created):
    row = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol=pos["symbol"],
        right=pos["right"],
        contract=pos["contract"],
        expiry=pos["expiry"],
        strike=pos["strike"],
        quantity=1,
        limit_price=0.56,
        reason="stop loss -53% ≤ −50%",
        meta={"gate": "exit", "reject_kind": "outside_hours", "queued_for_open": True, "retryable": True},
    )
    row.status = "deferred"
    row.error = "queued until 09:30 ET"
    row.created_at = created.astimezone(timezone.utc).isoformat()
    return row


def test_a_queued_exit_blocks_a_second_sell_after_the_close(tmp_path: Path, monkeypatch):
    at = session_et(16, 8)
    trader, broker, stop, _c, _p, pos, _buy = _stop_book(tmp_path, monkeypatch, at=at)
    stop.status = "cancelled"
    queued = _deferred(broker, pos, created=at - timedelta(minutes=1))
    broker.save()
    assert trader._sell_blocks(queued) is True
    assert trader._sell_already_working({"symbol": "MRK", "contract": pos["contract"]}, "C") is True
    # After 4 PM ET the queued exit is not resent; it waits for 9:30 ET.
    assert trader._queued_sells(at) == []


def test_duplicate_queued_exits_collapse_to_one_at_the_open(tmp_path: Path, monkeypatch):
    close = session_et(16, 8)
    trader, broker, stop, cancelled, placed, pos, _buy = _stop_book(tmp_path, monkeypatch, at=close)
    stop.status = "cancelled"
    rows = [_deferred(broker, pos, created=close + timedelta(minutes=i)) for i in range(16)]
    broker.save()
    nxt = session_et(9, 31, weekday_offset=1)
    out = trader._queued_sells(nxt)
    assert out == [rows[-1]]
    assert sum(1 for r in rows if r.status == "cancelled") == 15
    assert all("duplicate queued exit" in r.meta["cancel_reason"] for r in rows[:-1])


def test_stale_cancels_escalate_to_the_bid_then_marketable(tmp_path: Path, monkeypatch):
    at = session_et(10, 40)
    trader, broker, stop, _c, _p, pos, buy = _stop_book(tmp_path, monkeypatch, at=at)
    stop.status = "filled"  # not part of this walk
    stop.contract = "OTHER"
    contract = pos["contract"]
    assert trader._escalation_level(contract, buy) == 0
    for minute in (1, 2):
        sell = broker.stage(
            desk="golden_exit", action="SELL", symbol="MRK", right="C", contract=contract,
            expiry=pos["expiry"], strike=147, quantity=1, limit_price=0.3, reason="trail",
            meta={"gate": "exit"},
        )
        sell.status = "cancelled"
        sell.created_at = (at - timedelta(minutes=10 - minute)).astimezone(timezone.utc).isoformat()
        sell.meta["exit_clock"] = sell.created_at
        sell.meta["cancel_reason"] = "stale working sell — replace at a marketable limit"
        assert trader._escalation_level(contract, buy) == minute
