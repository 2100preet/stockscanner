"""How a Webull row should name its premium in the activity table."""

from __future__ import annotations

from typing import Any


def _money(value: Any) -> str | None:
    try:
        if value is None or value == "":
            return None
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return None


def activity_premium_label(order: dict[str, Any] | None) -> str:
    """``Limit (working) $1.05`` while the buy rests, ``Filled @ $1.22`` after a fill."""
    row = order or {}
    status = str(row.get("status") or "")
    fill = _money(row.get("fill_price"))
    limit = _money(row.get("limit_price"))
    if status == "partial" and fill and limit:
        return f"Filled @ {fill} · Limit (working) {limit}"
    if status in {"filled", "partial"} and fill:
        return f"Filled @ {fill}"
    if status == "filled" and limit:
        return f"Filled @ {limit}"
    if limit is None:
        return "—"
    if status in {"cancelled", "rejected", "skipped"}:
        return limit
    return f"Limit (working) {limit}"
