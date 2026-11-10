"""PR8: shared Webull call budget, detail rotation, and latency stamps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import odte_scanner.edge  # noqa: F401  (diamond <-> edge import order)
from odte_scanner.trading import webull
from odte_scanner.trading.latency import desk_key, latency_report, order_latency
from odte_scanner.trading.webull import EndpointBudget, WebullBroker, WebullOrderIntent, WebullRateLimit


class _Sleeps:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _live(tmp_path: Path, sleeper=None) -> WebullBroker:
    return WebullBroker(
        enabled=True,
        dry_run=False,
        app_key="k",
        app_secret="s",
        account_id="acct",
        ledger_path=tmp_path / "wb.json",
        submit_interval_sec=0,
        sleeper=sleeper or (lambda s: None),
    )


def _stage(broker: WebullBroker, n: int = 0, action: str = "BUY") -> WebullOrderIntent:
    return broker.stage(
        desk="golden_flow",
        action=action,
        symbol="SPY",
        right="C",
        contract=f"SPY261016C00{500 + n}000",
        expiry="2026-10-16",
        strike=500 + n,
        quantity=1,
        limit_price=1.0,
    )


# --- EndpointBudget ---------------------------------------------------------


def test_budget_allows_per_min_then_raises_for_reads_without_waiting(monkeypatch):
    budget = EndpointBudget(per_min=3)
    sleeps = _Sleeps()
    for _ in range(3):
        budget.reserve("detail", max_wait=0, sleeper=sleeps)
    with pytest.raises(WebullRateLimit, match="local Webull budget: detail"):
        budget.reserve("detail", max_wait=0, sleeper=sleeps)
    assert sleeps.sleeps == []
    # Endpoints are separate: place still has its own 3.
    budget.reserve("place", max_wait=0, sleeper=sleeps)
    assert budget.used("place") == 1 and budget.used("detail") == 3


def test_budget_order_call_waits_for_the_oldest_slot(monkeypatch):
    t = {"now": 100.0}
    monkeypatch.setattr(webull.time, "monotonic", lambda: t["now"])
    budget = EndpointBudget(per_min=2, window_sec=60)
    budget.reserve("place", max_wait=5, sleeper=lambda s: None)
    t["now"] = 157.0
    budget.reserve("place", max_wait=5, sleeper=lambda s: None)
    slept: list[float] = []

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        t["now"] += seconds

    budget.reserve("place", max_wait=5, sleeper=sleeper)  # first slot frees at 160
    assert slept and 3.0 <= slept[0] <= 3.1
    assert budget.used("place") == 2


def test_budget_order_call_raises_when_the_wait_is_too_long(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(webull.time, "monotonic", lambda: t["now"])
    budget = EndpointBudget(per_min=1, window_sec=60)
    budget.reserve("place", max_wait=5, sleeper=lambda s: None)
    t["now"] = 10.0
    with pytest.raises(WebullRateLimit):
        budget.reserve("place", max_wait=5, sleeper=lambda s: None)


def test_budget_wait_is_bounded_when_another_thread_takes_the_slot(monkeypatch):
    """A sleeper that never frees a slot (clock frozen) must not loop forever."""
    monkeypatch.setattr(webull.time, "monotonic", lambda: 59.0)
    budget = EndpointBudget(per_min=1, window_sec=60)
    budget._calls["place"] = [0.0]
    calls: list[float] = []
    with pytest.raises(WebullRateLimit):
        budget.reserve("place", max_wait=5, sleeper=calls.append)
    assert 1 <= len(calls) <= 200


def test_budget_disable_env(monkeypatch):
    monkeypatch.setenv("WEBULL_LOCAL_RATE_LIMIT", "0")
    budget = EndpointBudget(per_min=1)
    for _ in range(5):
        budget.reserve("detail", max_wait=0, sleeper=lambda s: None)
    assert budget.used("detail") == 0


def test_budget_per_min_env(monkeypatch):
    monkeypatch.setenv("WEBULL_SANDBOX_CALLS_PER_MIN", "12")
    assert webull._budget_per_min() == 12
    monkeypatch.setenv("WEBULL_SANDBOX_CALLS_PER_MIN", "junk")
    assert webull._budget_per_min() == 28


# --- submit under the budget -------------------------------------------------


def test_spent_place_budget_defers_without_any_http_call_and_retries_same_id(tmp_path, monkeypatch):
    broker = _live(tmp_path)
    posts: list[str] = []
    monkeypatch.setattr(broker, "_post_option", lambda intent: posts.append(intent.id) or f"WB-{len(posts)}")
    monkeypatch.setattr(webull, "ORDER_MAX_WAIT_SEC", 0.0)
    for _ in range(webull.WEBULL_BUDGET.per_min):
        webull.WEBULL_BUDGET.reserve("place", max_wait=0, sleeper=lambda s: None)
    intent = _stage(broker)
    broker.submit(intent)
    assert posts == []  # nothing reached Webull
    assert intent.status == "deferred" and intent.meta["retryable"] is True
    assert "sent_at" not in intent.meta and "first_sent_at" not in intent.meta
    webull.WEBULL_BUDGET.reset()
    broker.submit(intent)
    assert posts == [intent.id]  # same client order id, sent once
    assert intent.status == "submitted"
    assert len([o for o in broker.orders if o.action == "BUY"]) == 1


def test_submit_stamps_sent_and_acked_and_keeps_first_sent_on_retry(tmp_path, monkeypatch):
    broker = _live(tmp_path)
    tries = {"n": 0}

    def post(intent):
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("HTTP 429 TOO_MANY_REQUESTS")
        return "WB-1"

    monkeypatch.setattr(broker, "_post_option", post)
    intent = _stage(broker)
    broker.submit(intent)
    meta = intent.meta
    assert intent.status == "submitted"
    assert meta["first_sent_at"] <= meta["sent_at"] <= meta["acked_at"]
    assert tries["n"] == 2


def test_rejected_order_has_sent_but_no_ack(tmp_path, monkeypatch):
    broker = _live(tmp_path)

    def post(intent):
        raise RuntimeError('HTTP 417 {"error_code":"OPENAPI_ETF_OPTION_CAN_NOT_TRADING","message":"no"}')

    monkeypatch.setattr(broker, "_post_option", post)
    intent = _stage(broker)
    broker.submit(intent)
    assert intent.status == "rejected"
    assert intent.meta.get("sent_at") and "acked_at" not in intent.meta
    assert "sent→acked_s" not in order_latency(intent.to_dict())


def test_429_backoff_is_at_least_1_5s_even_with_fast_spacing(tmp_path, monkeypatch):
    sleeps = _Sleeps()
    broker = _live(tmp_path, sleeper=sleeps)
    tries = {"n": 0}

    def post(intent):
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("HTTP 429 TOO_MANY_REQUESTS")
        return "WB-1"

    monkeypatch.setattr(broker, "_post_option", post)
    broker.submit(_stage(broker))
    assert 1.5 in sleeps.sleeps


# --- detail rotation ---------------------------------------------------------


def _working(broker: WebullBroker, n: int) -> list[WebullOrderIntent]:
    rows = []
    for i in range(n):
        intent = _stage(broker, i)
        intent.status = "submitted"
        rows.append(intent)
    return rows


def test_detail_rotation_skips_over_budget_and_checks_skipped_ones_first(tmp_path, monkeypatch):
    monkeypatch.setattr(webull, "WEBULL_BUDGET", EndpointBudget(per_min=2))
    broker = _live(tmp_path)
    rows = _working(broker, 3)
    seen: list[str] = []

    def detail(intent):
        seen.append(intent.id)
        return {"status": "SUBMITTED"}

    monkeypatch.setattr(broker, "_fetch_order_detail", lambda intent: (webull.WEBULL_BUDGET.reserve("detail", max_wait=0, sleeper=lambda s: None), detail(intent))[1])
    out = broker.refresh_working_buys()
    assert len(out) == 3  # every working order is still returned
    assert len(seen) == 2
    first_pass = set(seen)
    assert all(r.status == "submitted" for r in rows)  # skipped ones stay working, not cancelled
    webull.WEBULL_BUDGET.reset()
    seen.clear()
    broker.refresh_working_buys()
    skipped = {r.id for r in rows} - first_pass
    assert seen[0] in skipped  # oldest-checked first


def test_detail_failure_leaves_the_order_working(tmp_path, monkeypatch):
    broker = _live(tmp_path)
    (row,) = _working(broker, 1)

    def boom(intent):
        raise WebullRateLimit("local Webull budget: detail used 28/60s")

    monkeypatch.setattr(broker, "_fetch_order_detail", boom)
    broker.refresh_working_buys()
    assert row.status == "submitted"


def test_filled_at_stamped_once_on_first_fill(tmp_path, monkeypatch):
    broker = _live(tmp_path)
    (row,) = _working(broker, 1)
    broker._apply_fill(row, {"fill_state": "partial", "filled_qty": 1, "fill_price": 1.0})
    first = row.meta["filled_at"]
    broker._apply_fill(row, {"fill_state": "filled", "filled_qty": 2, "fill_price": 1.0})
    assert row.meta["filled_at"] == first


def test_ledger_reload_keeps_latency_stamps(tmp_path, monkeypatch):
    """Restart mid-order: stamps survive a new broker on the same ledger."""
    broker = _live(tmp_path)
    monkeypatch.setattr(broker, "_post_option", lambda intent: "WB-1")
    intent = _stage(broker)
    broker.submit(intent)
    again = _live(tmp_path)
    row = next(o for o in again.orders if o.id == intent.id)
    assert row.meta["first_sent_at"] == intent.meta["first_sent_at"]
    assert row.meta["acked_at"] == intent.meta["acked_at"]


# --- latency report ----------------------------------------------------------


def _iso(base: datetime, sec: float) -> str:
    return (base + timedelta(seconds=sec)).isoformat()


def _row(desk, action, base, *, staged=0.0, sent=1.0, acked=1.2, filled=3.0, **meta):
    m = {"golden_kind": desk, "first_sent_at": _iso(base, sent), "sent_at": _iso(base, sent)}
    if acked is not None:
        m["acked_at"] = _iso(base, acked)
    if filled is not None:
        m["filled_at"] = _iso(base, filled)
    m.update(meta)
    return {"id": f"{desk}-{action}-{staged}", "desk": "golden_flow", "action": action, "symbol": "QQQ",
            "status": "filled", "created_at": _iso(base, staged), "meta": m}


def test_latency_report_medians_and_stage_math():
    base = datetime(2026, 11, 9, 14, 31, 30, tzinfo=timezone.utc)  # 08:31:30 CT
    rows = [
        _row("sniper", "BUY", base, sent=1, filled=2, detected_at=_iso(base, -0.5), signal_at_ct="08:30"),
        _row("sniper", "BUY", base, sent=3, filled=10, detected_at=_iso(base, -1.0), signal_at_ct="08:30"),
        _row("sniper", "SELL", base, sent=0.4, filled=9, exit_tier="sniper"),
    ]
    rep = latency_report(rows)
    sniper = rep["desks"]["sniper"]
    assert sniper["staged→sent_s"]["median"] == 2.0
    assert sniper["sent→filled_s"]["n"] == 2
    assert sniper["detected→staged_s"]["median"] == 0.8
    assert sniper["signal_bar→sent_s"]["median"] == 92.0  # bar 08:30:00 CT -> sent 08:31:32 CT
    assert rep["desks"]["exit:sniper"]["sent→filled_s"]["median"] == 8.6


def test_latency_prefers_webull_times_and_skips_restored_and_spx_and_dry_run():
    base = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    wb = _row("diamond", "BUY", base, sent=5, filled=50, wb_place_time_at=_iso(base, 2), wb_filled_time_at=_iso(base, 4))
    assert order_latency(wb)["sent→filled_s"] == 2.0
    restored = _row("diamond", "BUY", base, source="webull_history_import")
    assert "staged→sent_s" not in order_latency(restored)
    spx = dict(_row("sniper", "BUY", base), symbol="SPX")
    dry = dict(_row("sniper", "BUY", base), status="dry_run")
    assert latency_report([spx, dry])["desks"] == {}


def test_latency_since_filter_and_missing_stamps_are_not_guessed():
    base = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)
    old = _row("gold", "BUY", base)
    bare = {"id": "x", "action": "BUY", "status": "submitted", "created_at": base.isoformat(), "meta": {}}
    assert order_latency(bare) == {}
    assert latency_report([old], since=base + timedelta(days=1))["desks"] == {}


def test_desk_keys():
    assert desk_key({"action": "BUY", "meta": {"golden_kind": "sure_winner_gold"}}) == "gold"
    assert desk_key({"action": "SELL", "meta": {"exit_tier": "afternoon_puts"}}) == "exit:afternoon"
    assert desk_key({"action": "SELL", "desk": "expiry_close", "meta": {}}) == "expiry"


def test_routed_sniper_buy_carries_signal_stamps_and_chase_keeps_them(tmp_path):
    from tests.test_fast_desk import _broker, _card, _ct, _quote

    broker, trader = _broker(tmp_path)
    trader.sync_hot(sniper={"signals": [_card()]}, now=_ct(9, 16))
    (buy,) = [o for o in broker.orders if o.action == "BUY"]
    assert buy.meta["detected_at"] == "2026-10-30T14:15:40+00:00"
    assert buy.meta["signal_at_ct"] == "09:15"
    # Chase replacement (working, price moved) keeps the original signal stamps.
    buy.status = "submitted"
    buy.meta["fill_state"] = "working"
    buy.meta["chase_started_at"] = (_ct(9, 16)).astimezone(timezone.utc).isoformat()
    broker.save()
    trader.sync_hot(sniper=_quote(1.30), now=_ct(9, 17))
    repl = [o for o in broker.orders if o.action == "BUY" and o.id != buy.id]
    assert repl, "expected a chase reprice"
    assert repl[-1].meta["detected_at"] == "2026-10-30T14:15:40+00:00"
    assert repl[-1].meta["signal_at_ct"] == "09:15"
    rep = order_latency(buy.to_dict())
    assert "signal_bar→sent_s" not in rep  # dry-run row was never sent: no guessed number
