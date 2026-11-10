"""PR8: /api/snapshot exit_health block."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import odte_scanner.edge  # noqa: F401
from odte_scanner.trading.exit_health import build_exit_health, flatten_lateness, stuck_sells

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _lot(contract, entry, exit_px, *, day, kind="sniper", qty=1, buy_meta=None, sell_meta=None, reason="target", n=0):
    at = datetime.fromisoformat(f"{day}T14:{30 + n:02d}:00+00:00")
    out_at = at + timedelta(minutes=20)
    sym = contract[:3]
    buy = {"id": f"b-{contract}-{day}-{n}", "action": "BUY", "symbol": sym, "contract": contract, "status": "filled",
           "fill_price": entry, "limit_price": entry, "quantity": qty, "desk": "golden_flow",
           "created_at": _iso(at), "updated_at": _iso(at), "meta": {"golden_kind": kind, "filled_qty": qty, **(buy_meta or {})}}
    sell = {"id": f"s-{contract}-{day}-{n}", "action": "SELL", "symbol": sym, "contract": contract, "status": "filled",
            "fill_price": exit_px, "limit_price": exit_px, "quantity": qty, "desk": "golden_exit", "reason": reason,
            "created_at": _iso(out_at), "updated_at": _iso(out_at),
            "meta": {"golden_kind": kind, "exit_tier": kind, "filled_qty": qty, "entry_price": entry,
                     "closes_order_id": buy["id"], **(sell_meta or {})}}
    return [buy, sell]


def test_empty_ledger_is_safe_and_not_ready():
    out = build_exit_health([], now=NOW)
    assert out["stuck_sells"]["n"] == 0
    assert out["profit_factor"]["window"] is None
    assert out["checks"]["flatten_p90_within_60s"]["ok"] is False  # no data is not a pass
    assert out["go_live_ready"] is False
    assert build_exit_health(None, now=NOW)["sessions"]["n"] == 0


def test_stuck_sell_rules():
    rows = [
        {"id": "a", "action": "SELL", "symbol": "QQQ", "status": "staged", "created_at": _iso(NOW - timedelta(seconds=30))},
        {"id": "b", "action": "SELL", "symbol": "QQQ", "status": "staged", "created_at": _iso(NOW - timedelta(seconds=5))},
        {"id": "c", "action": "SELL", "symbol": "QQQ", "status": "submitted", "created_at": _iso(NOW - timedelta(minutes=11))},
        {"id": "d", "action": "SELL", "symbol": "QQQ", "status": "deferred", "created_at": _iso(NOW - timedelta(minutes=3))},
        {"id": "e", "action": "SELL", "symbol": "QQQ", "status": "deferred", "meta": {"queued_for_open": True},
         "created_at": _iso(NOW - timedelta(hours=3))},  # market closed: queued for the open, not stuck
        {"id": "f", "action": "SELL", "symbol": "SPX", "status": "staged", "created_at": _iso(NOW - timedelta(hours=1))},
        {"id": "g", "action": "SELL", "symbol": "QQQ", "status": "staged", "created_at": _iso(NOW - timedelta(days=8))},
        {"id": "h", "action": "BUY", "symbol": "QQQ", "status": "staged", "created_at": _iso(NOW - timedelta(hours=1))},
    ]
    assert sorted(r["id"] for r in stuck_sells(rows, NOW)) == ["a", "c", "d"]


def test_loss_vs_stop_uses_stamped_plan_then_desk_default_and_counts_lots():
    rows = []
    rows += _lot("QQQ261109C00600000", 1.00, 0.11, day="2026-11-09", kind="sniper")                 # -89% vs -40
    rows += _lot("SPY261109C00700000", 1.00, 0.55, day="2026-11-09", kind="diamond", n=1)           # -45% ok (tol 10)
    rows += _lot("NVD261120C00200000", 2.00, 0.40, day="2026-11-06", kind="sure_winner_gold",
                 buy_meta={"planned_stop_pct": 50.0}, n=2)                                          # -80% vs -50
    rows += _lot("AMD261109C00250000", 1.00, 1.50, day="2026-11-09", n=3)                          # winner
    out = build_exit_health(rows, now=NOW)["loss_vs_planned_stop"]
    assert out["losing_lots"] == 3
    assert out["beyond_stop"] == 2
    worst = out["worst"]
    assert worst[0]["contract"].startswith("QQQ") and worst[0]["planned_from"] == "desk default"
    nvd = next(r for r in worst if r["contract"].startswith("NVD"))
    assert nvd["planned_from"] == "stamped" and nvd["planned_stop_pct"] == -50.0


def test_scale_out_lot_is_judged_as_one_lot():
    buy, s1 = _lot("QQQ261109C00600000", 1.00, 1.60, day="2026-11-09", qty=2)
    s1["quantity"] = 1
    s1["meta"]["filled_qty"] = 1
    s2 = dict(s1, id="s2", fill_price=0.50, limit_price=0.50, meta=dict(s1["meta"]))
    out = build_exit_health([buy, s1, s2], now=NOW)["loss_vs_planned_stop"]
    assert out["losing_lots"] == 0  # +60% and -50% halves net a winner


def _flat(reason, sent):
    return {"id": reason + sent.isoformat(), "action": "SELL", "symbol": "QQQ", "status": "filled", "reason": reason,
            "created_at": _iso(sent), "meta": {"first_sent_at": _iso(sent)}}


def test_flatten_deadline_parsing_ct_et_pm_and_outliers():
    rows = [
        _flat("sniper flatten 10:30 CT", datetime(2026, 11, 9, 16, 30, 30, tzinfo=timezone.utc)),   # +30s
        _flat("afternoon flatten 2:45 PM CT", datetime(2026, 11, 9, 20, 46, tzinfo=timezone.utc)),  # +60s
        _flat("time-stop 15:45 ET", datetime(2026, 11, 9, 20, 47, tzinfo=timezone.utc)),            # +120s
        _flat("gold flatten 14:30 CT", datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)),          # -30 min: ignored
        _flat("flatten 10:30 CT", datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)),               # hours later: ignored
        _flat("target hit", datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)),                      # not a flatten
    ]
    out = flatten_lateness(rows, NOW - timedelta(days=7))
    assert out["n"] == 3
    assert sorted(r["sent_after_deadline_s"] for r in out["latest"]) == [30, 60, 120]
    assert out["within_60s"] == 2
    assert out["median_s"] == 60.0


def test_flatten_prefers_webull_place_time():
    row = _flat("flatten 10:30 CT", datetime(2026, 11, 9, 16, 40, tzinfo=timezone.utc))
    row["meta"]["wb_place_time_at"] = _iso(datetime(2026, 11, 9, 16, 30, 10, tzinfo=timezone.utc))
    assert flatten_lateness([row], NOW - timedelta(days=1))["latest"][0]["sent_after_deadline_s"] == 10


def test_profit_factor_counts_expired_lots_and_handles_no_losses():
    rows = _lot("QQQ261109C00600000", 1.00, 2.00, day="2026-11-09")
    only_wins = build_exit_health(rows, now=NOW)
    assert only_wins["profit_factor"]["window"] is None  # never inf (invalid JSON)
    assert only_wins["checks"]["profit_factor_min_1.2"]["ok"] is True
    assert only_wins["checks"]["profit_factor_min_1.2"]["value"] == "no losses"
    expired = _lot("SPY261106C00700000", 1.00, 0.0, day="2026-11-06", n=1,
                   sell_meta={"expiry_close": True}, reason="expired worthless")
    expired[1]["desk"] = "expiry_close"
    expired[1]["meta"]["source_desk"] = "sniper"
    out = build_exit_health(rows + expired, now=NOW)
    assert out["profit_factor"]["window"] == 1.0  # +100 / -100
    assert out["checks"]["profit_factor_min_1.2"]["ok"] is False


def test_drawdown_sessions_and_window():
    rows = []
    rows += _lot("QQQ261102C00600000", 1.0, 3.0, day="2026-11-02")          # +200
    rows += _lot("QQQ261103C00600000", 1.0, 0.1, day="2026-11-03", n=1)     # -90
    rows += _lot("QQQ261104C00600000", 2.0, 0.5, day="2026-11-04", n=2)     # -150
    rows += _lot("QQQ261020C00600000", 1.0, 1.5, day="2026-10-20", n=3)     # +50, outside 7-day window
    out = build_exit_health(rows, now=NOW)
    assert out["drawdown"]["max_drawdown_usd"] == -240.0  # peak +250 -> +10
    assert out["drawdown"]["realized_usd"] == 10.0
    assert out["sessions"]["n"] == 4 and out["sessions"]["first"] == "2026-10-20"
    assert out["profit_factor"]["trades_in_window"] == 2  # rolling 7x24h: Nov 2 14:50Z is outside
    assert "equity history not available" in out["drawdown"]["source"]


def test_snapshot_overlay_carries_exit_health_and_latency(tmp_path):
    import json

    from odte_scanner.hot_state import apply_hot_overlay

    rows = _lot("QQQ261109C00600000", 1.0, 2.0, day="2026-11-09")
    body = apply_hot_overlay({"scores": []}, orders=rows, ledger_path=tmp_path / "wb.json")
    assert "checks" in body["exit_health"]
    assert "desks" in body["order_latency"]
    json.dumps(body["exit_health"], allow_nan=False)  # strict JSON: no inf / nan
    json.dumps(body["order_latency"], allow_nan=False)
