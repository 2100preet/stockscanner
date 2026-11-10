"""Signal -> sent: cycle schedule, stage stamps, and one entry per signal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import odte_scanner.edge  # noqa: F401
import odte_scanner.fast_desk as fd
from odte_scanner.fast_desk import next_cycle_sleep, run_fast_desk_cycle
from odte_scanner.trading.latency import latency_report, order_latency
from tests.test_fast_desk import _broker, _card, _ct


def _at(hh, mm, ss=0.0):
    base = datetime(2026, 11, 10, hh, mm, tzinfo=timezone.utc)
    return base + timedelta(seconds=ss)


# --- schedule ----------------------------------------------------------------


def test_period_subtracts_the_work_time():
    # 14:31:00 + 8 s of work, period 20 -> 12 s, next bar close (14:35:03) is later.
    assert next_cycle_sleep(_at(14, 31, 8), 8.0, 20.0) == 12.0


def test_wakes_just_after_the_bar_close_when_that_is_sooner():
    # 14:34:55, period would sleep 18 s; the 14:35 bar close + 3 s is 8 s away.
    assert abs(next_cycle_sleep(_at(14, 34, 55), 2.0, 20.0) - 8.0) < 1e-6


def test_long_cycle_still_sleeps_the_minimum():
    assert next_cycle_sleep(_at(14, 31, 0), 45.0, 20.0) == fd.MIN_SLEEP_SEC


def test_right_after_the_close_offset_waits_a_full_period_not_a_bar():
    # 14:35:03.5 is just past the wake point: the next close is ~5 min out.
    assert next_cycle_sleep(_at(14, 35, 3.5), 0.5, 20.0) == 19.5


def test_naive_now_is_treated_as_utc():
    naive = datetime(2026, 11, 10, 14, 34, 55)
    assert abs(next_cycle_sleep(naive, 2.0, 20.0) - 8.0) < 1e-6


def test_side_cycles_run_after_the_entries(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(fd, "in_hot_window", lambda now=None: True)
    monkeypatch.setattr(fd, "in_afternoon_window", lambda now=None: True)
    monkeypatch.setattr(fd, "run_fast_desk_cycle", lambda cfg: order.append("hot"))
    monkeypatch.setattr(fd, "run_afternoon_puts_cycle", lambda cfg: order.append("afternoon"))
    monkeypatch.setattr(fd, "run_lunch_credit_cycle", lambda cfg: order.append("lunch"))
    monkeypatch.setattr(fd, "_side_cycles", lambda: order.append("side"))
    assert fd._worker_pass(None) is True
    assert order == ["hot", "afternoon", "lunch", "side"]


def test_outside_windows_only_side_cycles_run(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(fd, "in_hot_window", lambda now=None: False)
    monkeypatch.setattr(fd, "in_afternoon_window", lambda now=None: False)
    monkeypatch.setattr(fd, "run_fast_desk_cycle", lambda cfg: order.append("hot"))
    monkeypatch.setattr(fd, "_side_cycles", lambda: order.append("side"))
    monkeypatch.setattr(fd, "publish_index_boards_if_missing", lambda: order.append("publish"))
    assert fd._worker_pass(None) is False
    assert order == ["side", "publish"]


# --- stamps ------------------------------------------------------------------


def test_cycle_and_route_stamps_reach_the_order(tmp_path):
    broker, trader = _broker(tmp_path)
    card = _card()
    out = run_fast_desk_cycle(
        now=_ct(9, 16),
        boards=({"signals": []}, {"signals": [card]}),
        dispatch=lambda _s: {"ok": True, "sent": 0},
        route=lambda d, s, now: trader.sync_hot(diamond=d, sniper=s, now=now),
    )
    assert set(out["timing"]) == {"boards_sec", "route_sec", "total_sec"}
    (buy,) = [o for o in broker.orders if o.action == "BUY"]
    meta = buy.meta
    for key in ("cycle_started_at", "boards_ready_at", "route_started_at", "quote_ready_at", "detected_at", "signal_at_ct"):
        assert meta.get(key), key
    assert meta["cycle_started_at"] <= meta["boards_ready_at"] <= meta["route_started_at"] <= meta["quote_ready_at"] <= buy.created_at


def _row(**meta):
    base = {"golden_kind": "sniper", "signal_at_ct": "08:50"}
    base.update(meta)
    return {"id": "x", "action": "BUY", "symbol": "SPY", "status": "filled",
            "created_at": meta.pop("_staged", None) or "2026-11-09T14:50:19+00:00", "meta": base}


def test_stage_breakdown_math():
    row = _row(
        detected_at="2026-11-09T14:50:10+00:00",
        cycle_started_at="2026-11-09T14:50:10+00:00",
        boards_ready_at="2026-11-09T14:50:16+00:00",
        route_started_at="2026-11-09T14:50:17+00:00",
        quote_ready_at="2026-11-09T14:50:18.500000+00:00",
        first_sent_at="2026-11-09T14:50:19.400000+00:00",
    )
    lat = order_latency(row)
    assert lat["signal_bar→detected_s"] == 10.0
    assert lat["detected→cycle_s"] == 0.0
    assert lat["cycle→boards_s"] == 6.0
    assert lat["boards→route_s"] == 1.0
    assert lat["route→quote_s"] == 1.5
    assert lat["quote→staged_s"] == 0.5
    assert round(lat["staged→sent_s"], 1) == 0.4
    assert round(lat["bar_close→sent_s"], 1) == -280.6  # forming-bar desk: sent before the bar closed


def test_chase_replacement_has_no_signal_side_stages():
    row = _row(replaces="wb-1", detected_at="2026-11-09T14:50:10+00:00", first_sent_at="2026-11-09T14:51:57+00:00")
    lat = order_latency(row)
    assert "signal_bar→sent_s" not in lat and "signal_bar→detected_s" not in lat
    assert "staged→sent_s" not in lat or lat["staged→sent_s"] >= 0


def test_report_lists_every_stage_name():
    rep = latency_report([])
    assert "bar_close→sent_s" in rep["stages"] and "cycle→boards_s" in rep["stages"]


# --- one entry per signal ----------------------------------------------------


def _signal(strike: float, ask: float = 1.22, **extra) -> dict:
    contract = f"SPY261030P00{int(strike * 1000):06d}"
    return {**_card(), "strike": strike, "contract": contract, "ask": ask, "send_order": True, **extra}


def test_spot_move_to_a_new_strike_does_not_buy_the_signal_twice(tmp_path):
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    trader.sync_hot(sniper={"signals": [_signal(760)]}, now=_ct(9, 17))
    buys = [o for o in broker.orders if o.action == "BUY"]
    assert [o.strike for o in buys] == [759]


def test_cancelled_entry_does_not_reopen_on_another_strike(tmp_path):
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    (buy,) = [o for o in broker.orders if o.action == "BUY"]
    buy.status = "cancelled"
    buy.meta["chase_abandoned"] = True
    broker.save()
    trader.sync_hot(sniper={"signals": [_signal(760)]}, now=_ct(9, 17))
    assert len([o for o in broker.orders if o.action == "BUY"]) == 1


def test_a_new_signal_bar_or_another_day_still_enters(tmp_path):
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    # A different signal bar the same day is a different signal.
    trader.sync_hot(sniper={"signals": [_signal(758, at_ct="09:40")]}, now=_ct(9, 41))
    # Yesterday's 09:15 row does not block today's 09:15 signal.
    old = next(o for o in broker.orders if o.action == "BUY" and o.strike == 759)
    old.meta["signal_day"] = "2026-10-29"
    broker.save()
    trader.sync_hot(sniper={"signals": [_signal(757)]}, now=_ct(9, 42))
    strikes = sorted(o.strike for o in broker.orders if o.action == "BUY")
    assert strikes == [757, 758, 759]


def test_retryable_reject_does_not_block_the_signal(tmp_path):
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    (buy,) = [o for o in broker.orders if o.action == "BUY"]
    buy.status = "rejected"
    buy.meta["retryable"] = True
    broker.save()
    assert trader._signal_already_routed("sniper", "SPY", "P", _signal(760)) is False


def test_legacy_rows_match_on_the_reason_text(tmp_path):
    broker, trader = _broker(tmp_path)
    trader._now = _ct(9, 20)
    intent = broker.stage(desk="sniper", action="BUY", symbol="SPY", right="P", contract="SPY261030P00759000",
                          expiry="2026-10-30", strike=759, quantity=1, limit_price=1.0,
                          reason="Sniper · retest · signal 09:15 CT · flat 10:30 CT.", meta={"golden_kind": "sniper"})
    intent.status = "filled"
    intent.created_at = _ct(9, 16).astimezone(timezone.utc).isoformat()
    assert trader._signal_already_routed("sniper", "SPY", "P", _signal(760)) is True
    assert trader._signal_already_routed("sniper", "SPY", "C", _signal(760)) is False  # other side
    assert trader._signal_already_routed("diamond", "SPY", "P", _signal(760)) is False  # other desk


def test_already_routed_signal_makes_no_quote_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("odte_scanner.options.contract_quote.fetch_contract_quote",
                        lambda row: calls.append(row.get("contract")) or None)
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    first = len(calls)
    for minute in (17, 18, 19):
        trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, minute))
    assert first >= 1 and len(calls) == first


def test_chase_replacement_is_not_blocked_by_the_signal_check(tmp_path):
    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_signal(759)]}, now=_ct(9, 16))
    (buy,) = [o for o in broker.orders if o.action == "BUY"]
    buy.status = "submitted"
    buy.meta["fill_state"] = "working"
    buy.meta["chase_started_at"] = _ct(9, 16).astimezone(timezone.utc).isoformat()
    broker.save()
    trader.sync_hot(sniper={"signals": [{**_signal(759, ask=1.30), "send_order": False}]}, now=_ct(9, 17))
    repl = [o for o in broker.orders if o.action == "BUY" and o.meta.get("replaces") == buy.id]
    assert len(repl) == 1
