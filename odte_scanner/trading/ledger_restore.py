"""Put filled Webull paper rows back into the ledger from a backup copy.

The ledger file used to keep only the newest 500 rows, so on Nov 9 it dropped
15 filled buys from Oct 20-26 (and the expiry closes made for them). Webull's
order-history endpoint only reaches back about 7 days, so those rows cannot be
re-imported from Webull. This restores them from a saved copy of the ledger.

Only rows that look like real Webull fills come back:
  - status filled, meta.broker_status FILLED, with the Webull order payload;
  - filled_qty equal to the order quantity;
  - not already in the ledger (same id or same broker order id).
Rows already present are never touched. Dry run unless ``apply=True``.

  python -m odte_scanner.trading.ledger_restore BACKUP.json [--ledger PATH] [--apply]

Run it with the app's ledger lock held (it takes the same sync lock as the
loops), on a ledger the new 'keep every fill' rule applies to, so the rows stay.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from odte_scanner.trading.webull import DEFAULT_LEDGER, WebullBroker, WebullOrderIntent


def _rows_from(path: Path) -> list[dict[str, Any]]:
    """Rows from a ledger copy: ``{"orders": [...]}`` or a bare list.

    A copy pulled off the machine can start with a non-JSON line (an ``ls -l``
    header); parsing then starts at the first line opening with ``{`` or ``[``.
    """
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except ValueError:
        lines = text.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.lstrip()[:1] in {"{", "["}), None)
        if start is None:
            raise
        raw = json.loads("".join(lines[start:]))
    rows = raw.get("orders") if isinstance(raw, dict) else raw
    return [r for r in rows or [] if isinstance(r, dict)]


def _why_not(row: dict[str, Any]) -> str | None:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    if str(row.get("status") or "") != "filled":
        return "not filled"
    if str(meta.get("broker_status") or "").upper() != "FILLED":
        return f"broker_status {meta.get('broker_status')!r}"
    if not meta.get("openapi_order"):
        return "no Webull order payload"
    try:
        filled = float(meta.get("filled_qty"))
        qty = float(row.get("quantity"))
    except (TypeError, ValueError):
        return "no filled quantity"
    if abs(filled - qty) > 1e-6:
        return f"filled_qty {filled:g} != quantity {qty:g}"
    if str(row.get("symbol") or "").upper() == "SPX":
        return "SPX is never routed to Webull"
    return None


def plan_restore(ledger: list[WebullOrderIntent], backup: list[dict[str, Any]], *, ids: set[str] | None = None) -> dict[str, Any]:
    have_ids = {o.id for o in ledger}
    have_broker = {str(o.broker_order_id) for o in ledger if o.broker_order_id}
    add: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in backup:
        rid = str(row.get("id") or "")
        if not rid or (ids is not None and rid not in ids):
            continue
        if rid in have_ids or (row.get("broker_order_id") and str(row["broker_order_id"]) in have_broker):
            continue
        why = _why_not(row)
        if why:
            if ids is not None or str(row.get("status") or "") == "filled":
                skipped.append({"id": rid, "contract": row.get("contract"), "why": why})
            continue
        add.append(row)
    return {"add": add, "skipped": skipped}


def restore(
    backup_path: str | Path,
    *,
    ledger_path: str | Path | None = None,
    apply: bool = False,
    ids: set[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    from odte_scanner.trading.auto_trader import _SYNC_LOCK

    backup = _rows_from(Path(backup_path))
    allowed = {f.name for f in fields(WebullOrderIntent)}
    with _SYNC_LOCK:
        broker = WebullBroker(ledger_path=ledger_path or DEFAULT_LEDGER)
        # Keep the file header as the app wrote it (this broker is not the trading one).
        try:
            head = json.loads(Path(broker.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            head = {}
        for key in ("enabled", "dry_run", "sandbox", "paper"):
            if isinstance(head, dict) and key in head:
                setattr(broker, key, bool(head[key]))
        plan = plan_restore(broker.orders, backup, ids=ids)
        stamp = note or f"restored from ledger backup {Path(backup_path).name} at {datetime.now(timezone.utc).isoformat()}"
        if apply and plan["add"]:
            for row in plan["add"]:
                intent = WebullOrderIntent(**{k: v for k, v in row.items() if k in allowed})
                if not isinstance(intent.meta, dict):
                    intent.meta = {}
                intent.meta["restored"] = stamp
                broker.orders.append(intent)
            broker.orders.sort(key=lambda o: str(o.created_at or ""), reverse=True)
            broker.save()
    return {
        "applied": bool(apply and plan["add"]),
        "added": [{"id": r["id"], "contract": r.get("contract"), "qty": r.get("quantity"), "fill": r.get("fill_price")} for r in plan["add"]],
        "skipped": plan["skipped"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("backup")
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--ids", default=None, help="comma-separated row ids to restore (default: every eligible missing fill)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    ids = {x.strip() for x in args.ids.split(",") if x.strip()} if args.ids else None
    print(json.dumps(restore(args.backup, ledger_path=args.ledger, apply=args.apply, ids=ids), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
