"""A gold stop rests at the stop, but not forever once the bid gaps under it.

Nov 10: QUBT 6.5C stop rested at Limit $0.12 for hours with the bid at $0.08.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path

import odte_scanner.edge  # noqa: F401  load first: signals.diamond <-> edge import cycle
from odte_scanner.trading.auto_trader import HARD_STOP_REST_SEC
from tests.session_clock import iso_day, session_et
from tests.test_gold_exits import _arm, _broker, _install, _pos, _sync, _trader


def _setup(tmp_path: Path, monkeypatch, *, rested_sec: float, mark: float = 0.08):
    morning = session_et(11, 0)
    weekly = iso_day(session_et(11, 0, weekday_offset=3))
    broker = _broker(tmp_path, "live.json", live=True)
    trader = _trader(broker)
    pos = _pos("QUBT", 6.5, weekly, cost=0.25, mark=mark, quantity=3, position_id="pos-qubt")
    buy = _arm(broker, pos)
    buy.created_at = (morning - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    stop = broker.stage(
        desk="golden_exit",
        action="SELL",
        symbol="QUBT",
        right="C",
        contract=pos["contract"],
        expiry=weekly,
        strike=6.5,
        quantity=3,
        limit_price=0.12,
        reason="stop loss -52% ≤ −50%",
        meta={"gate": "exit", "hard_stop": True},
    )
    stop.status = "submitted"
    stop.broker_order_id = "WB-STOP"
    stop.created_at = (morning - timedelta(seconds=rested_sec)).astimezone(timezone.utc).isoformat()
    stop.meta["exit_clock"] = stop.created_at
    broker.save()
    cancelled: list[str] = []
    placed: list = []
    monkeypatch.setattr(
        broker,
        "_fetch_order_detail",
        lambda order: {"order_status": "SUBMITTED", "filled_quantity": "0", "quantity": "3"},
    )
    monkeypatch.setattr(broker, "_cancel_via_openapi", lambda order: cancelled.append(order.id))
    monkeypatch.setattr(broker, "_place_via_openapi", lambda order: placed.append(order) or "SELL-NEW")
    _install(monkeypatch, broker, lambda: [pos])
    return trader, morning, stop, cancelled, placed, pos


def test_a_fresh_stop_keeps_resting_at_the_stop(tmp_path: Path, monkeypatch):
    trader, morning, stop, cancelled, placed, _ = _setup(tmp_path, monkeypatch, rested_sec=HARD_STOP_REST_SEC - 60)
    _sync(trader, morning)
    assert cancelled == []
    assert placed == []
    assert not stop.meta.get("hard_stop_timed_out")


def test_a_gapped_stop_is_replaced_at_the_bid_after_the_rest(tmp_path: Path, monkeypatch):
    trader, morning, stop, cancelled, placed, _ = _setup(tmp_path, monkeypatch, rested_sec=HARD_STOP_REST_SEC + 60)
    _sync(trader, morning)
    assert cancelled == [stop.id]
    assert stop.meta["hard_stop_timed_out"] is True
    assert "above the bid" in stop.meta["cancel_reason"]
    assert len(placed) == 1
    assert placed[0].limit_price == 0.08
    assert not placed[0].meta.get("hard_stop")
    assert placed[0].quantity == 3


def test_an_old_stop_the_bid_can_fill_is_left_alone(tmp_path: Path, monkeypatch):
    # The bid is back at the stop: it can fill there, so do not walk it.
    trader, morning, stop, cancelled, placed, _ = _setup(
        tmp_path, monkeypatch, rested_sec=HARD_STOP_REST_SEC * 4, mark=0.12
    )
    _sync(trader, morning)
    assert cancelled == []
    assert not stop.meta.get("hard_stop_timed_out")


def test_rest_uses_the_trader_clock_not_wall_time(tmp_path: Path, monkeypatch):
    # Staged on the trader's clock a minute ago, even if wall time says hours.
    trader, morning, stop, cancelled, placed, _ = _setup(tmp_path, monkeypatch, rested_sec=HARD_STOP_REST_SEC * 10)
    stop.meta["exit_clock"] = (morning - timedelta(seconds=60)).astimezone(timezone.utc).isoformat()
    trader.broker.save()
    _sync(trader, morning)
    assert cancelled == []
