"""Webull paper fills are the source of truth: the ledger file never drops one.

The old newest-500 rule dropped 15 filled Oct 20-26 buys (and their expiry
closes) on Nov 9. Only non-filled noise is capped now, and ledger_restore puts
dropped fills back from a saved copy.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from odte_scanner.trading import ledger_restore
from odte_scanner.trading.webull import WebullBroker, WebullOrderIntent, orders_for_ledger

T0 = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)


def _row(i, *, status="rejected", action="BUY", symbol="AAPL", qty=1, meta=None, minutes=None):
    when = (T0 + timedelta(minutes=i if minutes is None else minutes)).isoformat()
    return WebullOrderIntent(
        id=f"wb-{i:05d}", desk="sniper", action=action, symbol=symbol, right="C",
        contract=f"{symbol}261023C00{100 + i % 50:03d}000", expiry="2026-10-23", strike=100.0,
        quantity=qty, limit_price=1.0, fill_price=1.0 if status == "filled" else None,
        status=status, created_at=when, updated_at=when, meta=dict(meta or {}),
    )


def _newest_first(rows):
    return sorted(rows, key=lambda o: o.created_at, reverse=True)


def test_under_limit_keeps_everything_in_order():
    rows = _newest_first([_row(i) for i in range(10)])
    assert [o.id for o in orders_for_ledger(rows, limit=500)] == [o.id for o in rows]


def test_old_fills_and_open_orders_survive_a_busy_tape():
    old_fills = [_row(i, status="filled", meta={"broker_status": "FILLED"}) for i in range(15)]
    partial = _row(20, status="partial")
    cancelled_after_fill = _row(21, status="cancelled", meta={"filled_qty": 1})
    fill_state = _row(22, status="cancelled", meta={"fill_state": "filled"})
    old_open = _row(23, status="submitted")
    noise = [_row(1000 + i, status="rejected") for i in range(800)]
    rows = _newest_first(old_fills + [partial, cancelled_after_fill, fill_state, old_open] + noise)
    kept = orders_for_ledger(rows, limit=500)
    ids = {o.id for o in kept}
    for keep in old_fills + [partial, cancelled_after_fill, fill_state, old_open]:
        assert keep.id in ids, keep.id
    kept_noise = [o for o in kept if o.status == "rejected"]
    assert len(kept_noise) == 500
    # the newest noise is what stays
    assert {o.id for o in kept_noise} == {o.id for o in _newest_first(noise)[:500]}
    # order is preserved (newest first)
    assert kept == [o for o in rows if o.id in ids]


def test_bad_filled_qty_meta_is_noise_not_a_crash():
    rows = _newest_first([_row(i, status="cancelled", meta={"filled_qty": "n/a"}) for i in range(5)])
    assert len(orders_for_ledger(rows, limit=2)) == 2


def test_save_round_trip_keeps_every_fill(tmp_path: Path):
    path = tmp_path / "wb.json"
    broker = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    fills = [_row(i, status="filled") for i in range(20)]
    noise = [_row(2000 + i) for i in range(600)]
    broker.orders = _newest_first(fills + noise)
    broker.save()
    again = WebullBroker(enabled=False, dry_run=True, ledger_path=path)
    ids = {o.id for o in again.orders}
    assert all(f.id in ids for f in fills)
    assert len(again.orders) == 520


# ---- ledger_restore -------------------------------------------------------

def _fill_dict(i, *, qty=1, filled_qty=None, broker_status="FILLED", symbol="AAPL", payload=True, status="filled"):
    row = _row(i, status=status, symbol=symbol, qty=qty).to_dict()
    row["broker_order_id"] = f"B{i}"
    row["meta"] = {
        "broker_status": broker_status,
        "filled_qty": qty if filled_qty is None else filled_qty,
    }
    if payload:
        row["meta"]["openapi_order"] = {"order_id": f"B{i}"}
    return row


def _write_ledger(path: Path, rows, *, enabled=True, dry_run=False):
    path.write_text(json.dumps({"enabled": enabled, "dry_run": dry_run, "sandbox": True, "paper": True,
                                "orders": rows}))


def test_restore_dry_run_apply_skip_rules_and_idempotent(tmp_path: Path):
    ledger = tmp_path / "wb.json"
    present = _fill_dict(1)
    _write_ledger(ledger, [present] + [_row(3000 + i).to_dict() for i in range(600)])
    backup = tmp_path / "backup.json"
    good = _fill_dict(2)
    dup_broker_id = dict(_fill_dict(3), broker_order_id="B1")  # same Webull order as a present row
    rows = [
        present,                                   # already in ledger: untouched, not listed
        good,                                      # restored
        dup_broker_id,                             # same broker order id: not restored
        _fill_dict(4, qty=2, filled_qty=3),        # filled_qty != quantity
        _fill_dict(5, broker_status="SUBMITTED"),  # Webull never confirmed the fill
        _fill_dict(6, symbol="SPX"),               # SPX is never routed to Webull
        _fill_dict(7, payload=False),              # no Webull order payload
        _row(8, status="rejected").to_dict(),      # not a fill: ignored silently
    ]
    backup.write_text("-rw-r--r-- 1 app app 12345 Nov  9 20:28 wb.json\n" + json.dumps({"orders": rows}))
    before = ledger.read_text()

    dry = ledger_restore.restore(backup, ledger_path=ledger)
    assert dry["applied"] is False
    assert [a["id"] for a in dry["added"]] == [good["id"]]
    why = {s["id"]: s["why"] for s in dry["skipped"]}
    assert why == {
        "wb-00004": "filled_qty 3 != quantity 2",
        "wb-00005": "broker_status 'SUBMITTED'",
        "wb-00006": "SPX is never routed to Webull",
        "wb-00007": "no Webull order payload",
    }
    assert ledger.read_text() == before  # dry run writes nothing

    done = ledger_restore.restore(backup, ledger_path=ledger, apply=True, note="test restore")
    assert done["applied"] is True and [a["id"] for a in done["added"]] == [good["id"]]
    saved = json.loads(ledger.read_text())
    assert saved["enabled"] is True and saved["dry_run"] is False  # header kept as the app wrote it
    by_id = {r["id"]: r for r in saved["orders"]}
    assert by_id[good["id"]]["meta"]["restored"] == "test restore"
    assert by_id[good["id"]]["status"] == "filled"
    assert present["id"] in by_id
    assert "wb-00006" not in by_id
    # sorted newest first, restored Oct row sits among the old rows, fills kept past the 500 cap
    created = [r["created_at"] for r in saved["orders"]]
    assert created == sorted(created, reverse=True)
    assert sum(1 for r in saved["orders"] if r["status"] == "rejected") == 500

    again = ledger_restore.restore(backup, ledger_path=ledger, apply=True)
    assert again["added"] == [] and again["applied"] is False
    assert len(json.loads(ledger.read_text())["orders"]) == len(saved["orders"])


def test_restore_ids_filter(tmp_path: Path):
    ledger = tmp_path / "wb.json"
    _write_ledger(ledger, [])
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"orders": [_fill_dict(2), _fill_dict(3)]}))
    out = ledger_restore.restore(backup, ledger_path=ledger, apply=True, ids={"wb-00003"})
    assert [a["id"] for a in out["added"]] == ["wb-00003"]
    assert [r["id"] for r in json.loads(ledger.read_text())["orders"]] == ["wb-00003"]


def test_restore_cli_is_dry_run_by_default(tmp_path: Path, capsys):
    ledger = tmp_path / "wb.json"
    _write_ledger(ledger, [])
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps([_fill_dict(2)]))  # a bare list of rows works too
    assert ledger_restore.main([str(backup), "--ledger", str(ledger)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and len(out["added"]) == 1
    assert json.loads(ledger.read_text())["orders"] == []
