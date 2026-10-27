"""Append-only paper round trips for the Edge option-dollar table.

A sell fill writes one line. The 500-order Webull ledger can drop the buy
before the sell fills, so the entry is copied onto the sell when it is staged.
Reading this file does not invent trades that have not closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odte_scanner.edge.gate import source_for_webull

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "outputs" / "paper_round_trips.jsonl"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _entry_price(intent: Any, orders: list[Any]) -> float | None:
    meta = getattr(intent, "meta", None) or {}
    stamped = _num(meta.get("entry_price"))
    if stamped is not None:
        return stamped
    closes = str(meta.get("closes_order_id") or "")
    contract = str(getattr(intent, "contract", "") or "")
    for order in orders:
        if getattr(order, "action", "") != "BUY":
            continue
        if closes and getattr(order, "id", "") == closes:
            price = _num(getattr(order, "fill_price", None))
            if price is not None:
                return price
    for order in orders:
        if getattr(order, "action", "") != "BUY":
            continue
        if contract and str(getattr(order, "contract", "") or "") == contract:
            price = _num(getattr(order, "fill_price", None))
            if price is not None:
                return price
    return None


def _known_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    found: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id"):
            found.add(str(row["id"]))
    return found


def record_sell_fill(intent: Any, orders: list[Any] | None = None, *, path: Path | None = None) -> dict[str, Any] | None:
    """Append one Gold/desk round trip when a sell has an entry and an exit.

    Keyed by sell id plus filled quantity so a refresh of the same fill does
    not add a second sample. A later larger fill on the same sell is a new line.
    """
    if str(getattr(intent, "action", "") or "").upper() != "SELL":
        return None
    if str(getattr(intent, "status", "") or "") not in {"filled", "partial"}:
        return None
    exit_px = _num(getattr(intent, "fill_price", None))
    if exit_px is None:
        return None
    entry = _entry_price(intent, list(orders or []))
    if entry is None:
        return None
    meta = getattr(intent, "meta", None) or {}
    try:
        filled = int(float(meta.get("filled_qty") or getattr(intent, "quantity", None) or 0))
    except (TypeError, ValueError):
        filled = 0
    if filled <= 0:
        return None
    key = f"{intent.id}:{filled}"
    dest = path or PATH
    if key in _known_ids(dest):
        return None
    row = {
        "id": key,
        "sell_id": str(intent.id),
        "source": source_for_webull(str(getattr(intent, "desk", "") or "")),
        "contract": str(getattr(intent, "contract", "") or ""),
        "pnl": round((exit_px - entry) * 100.0, 4),
        "at": str(getattr(intent, "updated_at", "") or ""),
        "entry": entry,
        "exit": exit_px,
        "qty": filled,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return row


def load_round_trips(path: Path | None = None) -> list[dict[str, Any]]:
    dest = path or PATH
    if not dest.exists():
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = dest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "")
        if not key or key in seen:
            continue
        try:
            pnl = float(row["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(key)
        out.append(
            {
                "source": str(row.get("source") or "unknown"),
                "pnl": pnl,
                "at": str(row.get("at") or ""),
                "contract": str(row.get("contract") or ""),
            }
        )
    return out
