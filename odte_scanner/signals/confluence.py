"""⚡ Confluence: several desks agreeing on one symbol and direction. Display only.

An event is two or more distinct setups on the same symbol and direction
(calls or puts) inside a 15-minute window, read from today's sent-alert log.
The score is the number of distinct setups (⚡2, ⚡3 …). Lunch-Break Credit
sells a put spread, so it counts as bullish (calls). Afternoon Puts counts as
bearish (puts).

This module never routes, sizes, exits, caps, or mutes anything. Webull fills
are only read to show status and P&L. SPX stays alert-only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "confluence_day.json"
HISTORY_STORE = ROOT / "outputs" / "confluence_history.jsonl"
WINDOW = timedelta(minutes=15)
MIN_SCORE = 2
# An event older than this when first seen is recorded but not sent.
STALE = timedelta(minutes=20)
NOTE = (
    "Two or more desks on the same symbol and direction within 15 minutes. "
    "Display only: this table does not place, size, or exit any order."
)

# Customer-facing desk names. "Sure Winner" never reaches a customer.
_LABELS = {
    "sure winner": "Gold Setup",
    "sure winners": "Gold Setup",
    "gold setup": "Gold Setup",
    "golden flow": "Golden Flow",
    "lunchcredit": "Lunch-Break Credit",
    "lunch_credit": "Lunch-Break Credit",
    "lunch-break credit": "Lunch-Break Credit",
    "afternoonputs": "Afternoon Puts",
    "afternoon_puts": "Afternoon Puts",
    "afternoon puts": "Afternoon Puts",
    "must trade": "Must Trade",
}
_SKIP_KINDS = {"exit"}
_OCC = re.compile(r"^[A-Z]+W?\d{6}([CP])\d{8}$")


def setup_label(kind: Any) -> str:
    raw = str(kind or "").strip()
    key = raw.lower()
    if key in _LABELS:
        return _LABELS[key]
    if "sure winner" in key:
        return "Gold Setup"
    if "afternoon" in key:
        return "Afternoon Puts"
    if "lunch" in key:
        return "Lunch-Break Credit"
    return raw


def direction_of(row: dict[str, Any]) -> str | None:
    """'calls' (bullish) or 'puts' (bearish). None for exits and closes."""
    label = setup_label(row.get("kind"))
    if str(row.get("kind") or "").strip().lower() in _SKIP_KINDS:
        return None
    if label == "Lunch-Break Credit":
        return "calls"
    if label == "Afternoon Puts":
        return "puts"
    if str(row.get("side") or "BUY").upper() != "BUY":
        return None
    contract = str(row.get("contract") or "").upper()
    match = _OCC.match(contract)
    right = match.group(1) if match else None
    if right is None:
        parts = contract.split("|")
        if parts and parts[-1] in {"C", "P"}:
            right = parts[-1]
    if right == "C":
        return "calls"
    if right == "P":
        return "puts"
    return None


def _when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _hm(moment: datetime) -> str:
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}"


def find_events(alerts: list[dict[str, Any]], window: timedelta = WINDOW) -> list[dict[str, Any]]:
    """Group alerts into confluence events.

    Alerts on one symbol and direction chain into a cluster while each next
    alert is within ``window`` of the cluster's first alert. A cluster with
    two or more distinct setups is an event. Its score is that count.
    """
    legs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in alerts or []:
        if not isinstance(row, dict) or row.get("ok") is False:
            continue
        direction = direction_of(row)
        moment = _when(row.get("time"))
        symbol = str(row.get("symbol") or "").upper()
        if not direction or moment is None or not symbol:
            continue
        legs.setdefault((symbol, direction), []).append(
            {"setup": setup_label(row.get("kind")), "at": moment, "contract": row.get("contract")}
        )
    events: list[dict[str, Any]] = []
    for (symbol, direction), rows in legs.items():
        rows.sort(key=lambda leg: leg["at"])
        cluster: list[dict[str, Any]] = []
        for leg in rows + [None]:
            if leg is not None and cluster and leg["at"] - cluster[0]["at"] <= window:
                cluster.append(leg)
                continue
            if cluster:
                event = _event(symbol, direction, cluster)
                if event:
                    events.append(event)
            cluster = [leg] if leg is not None else []
    return sort_events(events)


def _event(symbol: str, direction: str, cluster: list[dict[str, Any]]) -> dict[str, Any] | None:
    first_by_setup: dict[str, dict[str, Any]] = {}
    for leg in cluster:
        first_by_setup.setdefault(leg["setup"], leg)
    if len(first_by_setup) < MIN_SCORE:
        return None
    setups = sorted(first_by_setup.values(), key=lambda leg: leg["at"])
    contract = next((leg["contract"] for leg in setups if leg.get("contract")), None)
    return {
        "symbol": symbol,
        "direction": direction,
        "score": len(setups),
        "badge": f"⚡{len(setups)}",
        "setups": [{"setup": leg["setup"], "at_ct": _hm(leg["at"]), "contract": leg.get("contract")} for leg in setups],
        "first_ct": _hm(cluster[0]["at"]),
        "last_ct": _hm(cluster[-1]["at"]),
        "first_at": cluster[0]["at"].isoformat(),
        "last_at": cluster[-1]["at"].isoformat(),
        "contract": contract,
        "contracts": sorted({str(leg["contract"]) for leg in cluster if leg.get("contract")}),
        "index_alert_only": symbol == "SPX",
    }


def sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda ev: (-int(ev.get("score") or 0), str(ev.get("first_at") or ""), ev.get("symbol") or ""))


def event_key(event: dict[str, Any]) -> str:
    return f"{event.get('symbol')}|{event.get('direction')}|{event.get('score')}"


def format_line(event: dict[str, Any]) -> str:
    """⚡ CONFLUENCE ⚡2 GOOGL CALLS · Sniper 8:55 + Diamond 9:01"""
    legs = " + ".join(f"{leg['setup']} {leg['at_ct']}" for leg in event.get("setups") or [])
    tail = " · SPX alert only" if event.get("index_alert_only") else ""
    return (
        f"⚡ CONFLUENCE {event.get('badge') or '⚡' + str(event.get('score'))} "
        f"{event.get('symbol')} {str(event.get('direction') or '').upper()} · {legs}{tail} · display only"
    )


# ---------------------------------------------------------------- paper fills


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def attach_fills(events: list[dict[str, Any]], orders: list[dict[str, Any]] | None) -> None:
    """Webull paper status and P&L for each event's contracts. Fills are the truth."""
    from odte_scanner.trading.labels import activity_premium_label

    book = [order for order in (orders or []) if isinstance(order, dict)]
    trips: list[dict[str, Any]] = []
    try:
        from odte_scanner.trading.portal_book import round_trips

        trips = round_trips(book, include_other_desks=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("confluence round trips failed: %s", exc)
    for event in events:
        wanted = set(event.get("contracts") or [])
        opens = [
            order
            for order in book
            if str(order.get("contract") or "") in wanted
            and (str((order.get("meta") or {}).get("role") or "") in {"", "open"})
            and (str(order.get("action") or "").upper() == "BUY" or str(order.get("desk") or "") == "lunch_credit")
        ]
        opens.sort(key=lambda order: str(order.get("created_at") or ""))
        order = opens[-1] if opens else None
        meta = (order or {}).get("meta") if isinstance((order or {}).get("meta"), dict) else {}
        bid = _num(meta.get("signal_bid", meta.get("bid"))) if order else None
        ask = _num(meta.get("signal_ask", meta.get("ask"))) if order else None
        pnl = [float(trip.get("pnl") or 0) for trip in trips if str(trip.get("contract") or "") in wanted]
        event["paper"] = {
            "contract": (order or {}).get("contract") or event.get("contract"),
            "status": str((order or {}).get("status") or ("alert only" if event.get("index_alert_only") else "no paper order")),
            "price_label": activity_premium_label(order) if order else "—",
            "bid": bid,
            "ask": ask,
            "quote_label": (f"Bid ${bid:.2f} / Ask ${ask:.2f}" if bid is not None and ask is not None else None),
            "realized_pnl_usd": round(sum(pnl), 2) if pnl else None,
            "closed_trips": len(pnl),
        }


def attach_em(events: list[dict[str, Any]], em_used: Callable[[str], float | None] | None = None) -> None:
    for event in events:
        value = None
        if em_used is not None:
            try:
                value = em_used(str(event.get("symbol")))
            except Exception:  # noqa: BLE001
                value = None
        event["em_used_pct"] = None if value is None else round(float(value), 1)


def _default_em_used(symbol: str) -> float | None:
    from odte_scanner.signals.expected_move import load_public_board

    for row in load_public_board().get("symbols") or []:
        if str(row.get("symbol")) == symbol:
            return _num(row.get("move_used_pct"))
    return None


# ---------------------------------------------------------------- board + cycle


def build_board(
    alerts: list[dict[str, Any]] | None = None,
    *,
    orders: list[dict[str, Any]] | None = None,
    day: str | None = None,
    em_used: Callable[[str], float | None] | None = _default_em_used,
    load_orders: bool = True,
) -> dict[str, Any]:
    if alerts is None:
        from odte_scanner.alert_pulse import read_alert_log

        log = read_alert_log(day=day, limit=500)
        alerts = log.get("alerts") or []
        day = day or log.get("day")
    if orders is None and load_orders:
        from odte_scanner.hot_state import read_ledger_orders

        orders = read_ledger_orders()
    events = find_events(alerts)
    attach_fills(events, orders)
    attach_em(events, em_used)
    return {
        "ok": True,
        "day": day or datetime.now(CT).date().isoformat(),
        "window_min": int(WINDOW.total_seconds() // 60),
        "n": len(events),
        "events": events,
        "note": NOTE,
        "display_only": True,
    }


def public_block() -> dict[str, Any]:
    """Snapshot block. A failure here never breaks the snapshot."""
    try:
        return build_board()
    except Exception as exc:  # noqa: BLE001
        logger.warning("confluence board failed: %s", exc)
        return {"ok": False, "events": [], "n": 0, "error": str(exc)[:200], "note": "Confluence unavailable."}


def read_state(path: Path | None = None) -> dict[str, Any]:
    try:
        blob = json.loads((path or DEFAULT_STORE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    dest = path or DEFAULT_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(state, default=str), encoding="utf-8")


def _archive(state: dict[str, Any], path: Path | None) -> None:
    if not state.get("day") or not state.get("events"):
        return
    dest = path or HISTORY_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"day": state["day"], "events": state["events"]}, default=str) + "\n")


def _default_send(text: str) -> dict[str, Any]:
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text)


def run_confluence_cycle(
    *,
    now: datetime | None = None,
    alerts: list[dict[str, Any]] | None = None,
    orders: list[dict[str, Any]] | None = None,
    store_path: Path | None = None,
    history_path: Path | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    em_used: Callable[[str], float | None] | None = _default_em_used,
) -> dict[str, Any]:
    """Persist today's events and send one line per new symbol+direction+score."""
    moment = (now or datetime.now(timezone.utc)).astimezone(CT)
    day = moment.date().isoformat()
    state = read_state(store_path)
    if str(state.get("day") or "") != day:
        _archive(state, history_path)
        state = {"day": day, "sent": []}
    board = build_board(alerts, orders=orders, day=day, em_used=em_used, load_orders=orders is None)
    sent = list(state.get("sent") or [])
    sender = send or _default_send
    lines: list[str] = []
    for event in sorted(board["events"], key=lambda ev: (str(ev.get("last_at")), ev.get("score"))):
        key = event_key(event)
        if key in sent:
            continue
        text = format_line(event)
        last = _when(event.get("last_at"))
        if last is not None and moment - last > STALE:
            # Backfill after a restart or deploy: record it, do not page.
            sent.append(key)
            continue
        try:
            result = sender(text) or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("confluence telegram failed: %s", exc)
            result = {"ok": False}
        # Mark as sent either way: at most one try per key per day.
        sent.append(key)
        lines.append(text)
        if not result.get("ok", True):
            logger.info("confluence telegram not ok for %s", key)
    state.update(sent=sent, events=board["events"], updated_at=moment.isoformat())
    write_state(state, store_path)
    board["sent_now"] = lines
    return board
