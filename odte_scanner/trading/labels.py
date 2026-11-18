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


def _is_live(order: dict[str, Any] | None) -> bool:
    row = order or {}
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    host = str(meta.get("host") or row.get("host") or "")
    return bool(
        meta.get("live")
        or meta.get("venue") == "webull_live"
        or row.get("venue") == "webull_live"
        or host == "api.webull.com"
    )


def activity_premium_label(order: dict[str, Any] | None) -> str:
    """``Limit $1.05 (working)`` while the buy rests, ``Filled @ $1.22`` after a fill.

    Live fills are labeled ``Filled @ $1.22 (Webull LIVE)``. Working live
    limits keep the same ``Limit $x (working)`` wording, with a separate LIVE tag.
    """
    row = order or {}
    status = str(row.get("status") or "")
    fill = _money(row.get("fill_price"))
    limit = _money(row.get("limit_price"))
    live = " (Webull LIVE)" if _is_live(row) else ""
    if status == "partial" and fill and limit:
        return f"Filled @ {fill}{live} · Limit {limit} (working)"
    if status in {"filled", "partial"} and fill:
        return f"Filled @ {fill}{live}"
    if status == "filled" and limit:
        return f"Filled @ {limit}{live}"
    if limit is None:
        return "—"
    if status in {"cancelled", "rejected", "skipped"}:
        return f"{limit}{live}" if live else limit
    return f"Limit {limit} (working)"
