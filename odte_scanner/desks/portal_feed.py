"""Shape Kotegawa and Million books for the main portal.

Read-only. Nothing here places an order, sizes a trade, or writes a ledger.
Fill alerts (BUY/SELL) stay on the board for the session day. Log-only
tickets (challenge quality, oversold bounce, the 1DTE twin) stay out of the
alert feed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.million import LOG_ONLY_SOURCES

CT = ZoneInfo("America/Chicago")
_FILL_SIDES = {"BUY", "SELL"}


def _today(now: datetime | None) -> str:
    if now is None:
        moment = datetime.now(CT)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=CT)
    else:
        moment = now.astimezone(CT)
    return moment.date().isoformat()


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _source(raw: Mapping[str, Any]) -> str:
    if raw.get("source"):
        return str(raw.get("source"))
    nested = raw.get("row")
    if isinstance(nested, Mapping) and nested.get("source"):
        return str(nested.get("source"))
    return ""


def _is_log_only(raw: Mapping[str, Any]) -> bool:
    if raw.get("log_only") is True:
        return True
    return _source(raw) in LOG_ONLY_SOURCES


def _alert_rows(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Persisted alerts first, then anything this cycle remembered.

    ``alerts`` is the ledger's recent list, so a fill does not disappear when
    the next cycle's ``cycle_alerts`` is empty. The same key is kept once.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bucket in (block.get("alerts"), block.get("cycle_alerts")):
        for raw in bucket or []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            found.append(raw)
    return found


def _feed_row(raw: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    nested = raw.get("row") if isinstance(raw.get("row"), Mapping) else {}
    side = str(raw.get("side") or raw.get("action") or "").upper()
    if side not in _FILL_SIDES:
        side = "BUY"
    symbol = raw.get("symbol") or nested.get("symbol")
    contract = raw.get("contract") or nested.get("contract")
    right = raw.get("right") if raw.get("right") is not None else nested.get("right")
    shares = raw.get("shares") if raw.get("shares") is not None else nested.get("shares")
    entry = _num(raw.get("entry") if raw.get("entry") is not None else nested.get("entry"))
    ask = _num(raw.get("ask"))
    bid = _num(raw.get("bid"))
    if side == "BUY" and ask is None:
        ask = entry
    if side == "SELL" and bid is None:
        bid = _num(raw.get("exit"))
    message = str(raw.get("message") or "")
    return {
        "key": raw.get("key"),
        "side": side,
        "action": "BUY_NOW" if side == "BUY" else "SELL_NOW",
        "desk": label,
        "label": label,
        "paper": True,
        "log_only": False,
        "symbol": symbol,
        "contract": contract,
        "expiry": raw.get("expiry") or nested.get("expiry"),
        "strike": raw.get("strike") if raw.get("strike") is not None else nested.get("strike"),
        "right": right,
        "shares": shares,
        "contracts": raw.get("contracts"),
        "entry": entry,
        "ask": ask,
        "bid": bid,
        "message": message,
        "detail": message,
        "session": str(raw.get("session") or "")[:10] or None,
    }


def _fills(block: Mapping[str, Any], *, label: str, today: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in _alert_rows(block):
        if _is_log_only(raw):
            continue
        side = str(raw.get("side") or raw.get("action") or "").upper()
        if side not in _FILL_SIDES:
            continue
        session = str(raw.get("session") or "")[:10]
        if session and session != today:
            continue
        symbol = raw.get("symbol")
        nested = raw.get("row") if isinstance(raw.get("row"), Mapping) else {}
        if not symbol and not nested.get("symbol"):
            continue
        out.append(_feed_row(raw, label=label))
    return out


def _position(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = _num(row.get("entry"))
    mark = _num(row.get("mark") if row.get("mark") is not None else row.get("last_mark"))
    unrealized = _num(row.get("unrealized_pnl"))
    shares = int(row.get("shares") or 0)
    contracts = int(row.get("contracts") or 0)
    if unrealized is None and entry is not None and mark is not None:
        if shares > 0:
            unrealized = round((mark - entry) * shares, 2)
        elif contracts > 0:
            unrealized = round((mark - entry) * 100.0 * contracts, 2)
    symbol = row.get("symbol")
    contract = row.get("contract")
    if not contract and symbol and shares:
        contract = f"{symbol} {shares} sh"
    return {
        "symbol": symbol,
        "contract": contract,
        "shares": shares or None,
        "contracts": contracts or None,
        "entry": entry,
        "mark": mark,
        "unrealized_pnl": unrealized,
        "right": row.get("right"),
        "expiry": row.get("expiry"),
        "source": row.get("source"),
    }


def _card(view: Mapping[str, Any], *, label: str, name: str, desk: str, doublings: bool) -> dict[str, Any]:
    opened = [_position(row) for row in view.get("open") or [] if isinstance(row, dict)]
    card: dict[str, Any] = {
        "desk": desk,
        "label": label,
        "name": name or view.get("name") or label,
        "paper": True,
        "equity": _num(view.get("equity")),
        "realized_today": _num(view.get("realized_today")),
        "realized_pnl": _num(view.get("realized_pnl")),
        "trades": int(view.get("trades") or 0),
        "wins": int(view.get("wins") or 0),
        "losses": int(view.get("losses") or 0),
        "win_rate": _num(view.get("win_rate")),
        "open_positions": int(view.get("open_positions") or len(opened)),
        "open": opened,
    }
    if doublings:
        card["doublings"] = _num(view.get("doublings"))
        card["target_doublings"] = _num(view.get("target_doublings"))
    return card


def _logs(block: Mapping[str, Any], *, today: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in block.get("log_only") or []:
        if not isinstance(row, dict) or row.get("log_only") is False:
            continue
        session = str(row.get("entry_session") or row.get("session") or "")[:10]
        if session and session != today and row.get("status") != "open":
            continue
        out.append(
            {
                "symbol": row.get("symbol"),
                "source": row.get("source"),
                "contract": row.get("contract"),
                "status": row.get("status"),
                "entry": _num(row.get("entry")),
                "exit_reason": row.get("exit_reason"),
                "pnl": _num(row.get("pnl")),
                "session": session or None,
                "log_only": True,
            }
        )
    return out


def shape_portal_feed(
    kotegawa: Mapping[str, Any] | None,
    million: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cards plus the day's fill alerts. Log-only tickets are not alerts."""
    kot = kotegawa if isinstance(kotegawa, Mapping) else {}
    mil = million if isinstance(million, Mapping) else {}
    today = _today(now)
    return {
        "day": today,
        "alerts": _fills(kot, label="[KOTEGAWA]", today=today) + _fills(mil, label="[MILLION]", today=today),
        "log_only": _logs(mil, today=today),
        "cards": [
            _card(kot, label="[KOTEGAWA]", name="Kotegawa", desk="kotegawa", doublings=False),
            _card(mil, label="[MILLION]", name="Million", desk="million_challenge", doublings=True),
        ],
    }


def snapshot_fields(view: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keys /api/snapshot copies for these two paper desks."""
    body = view if isinstance(view, Mapping) else {}
    portal = body.get("portal") if isinstance(body.get("portal"), Mapping) else {}
    rows = body.get("rows") or []
    return {
        "kotegawa": body.get("kotegawa") or {},
        "million_challenge": body.get("million_challenge") or {},
        "paper_desks": list(rows) if isinstance(rows, list) else [],
        "paper_portal": dict(portal),
    }
