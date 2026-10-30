"""First time a Sniper or Diamond signal was actually seen.

``at_ct`` is the closed 5-minute bar. ``detected_at`` is when this process
first had the bar, the window, and a live ask. A later scan must not move it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from odte_scanner.time_cst import CT


def format_ct_hms(stamp: datetime | str | None) -> str:
    """Clock in America/Chicago, ``H:MM:SS`` with no leading hour zero."""
    if stamp is None or stamp == "":
        return ""
    if isinstance(stamp, datetime):
        moment = stamp
    else:
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    ct = moment.astimezone(CT)
    return f"{ct.hour}:{ct.minute:02d}:{ct.second:02d}"


def bar_label(at_ct: str | None) -> str:
    """``09:15`` and ``9:15`` both become ``9:15``."""
    text = str(at_ct or "").strip()
    parts = text.split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[0])}:{int(parts[1]):02d}"
    return text


def signal_sent_line(at_ct: str | None, detected_at: datetime | str | None) -> str | None:
    """Customer line: ``Signal 9:15 bar · sent 9:15:40 CT``."""
    bar = bar_label(at_ct)
    sent = format_ct_hms(detected_at)
    if not bar or not sent:
        return None
    return f"Signal {bar} bar · sent {sent} CT"


def day_detected(store: dict[str, Any], day: str) -> dict[str, str]:
    if store.get("day") != day or not isinstance(store.get("detected"), dict):
        return {}
    out: dict[str, str] = {}
    for key, value in store["detected"].items():
        if value:
            out[str(key)] = str(value)
    return out


def remember_detected(
    book: dict[str, str],
    *,
    symbol: str,
    at_ct: str,
    side: str,
    now: datetime,
    active: bool,
) -> str | None:
    """Lock the first active sighting. Inactive passes keep a prior stamp."""
    key = f"{str(symbol).upper()}|{str(side or 'C').upper()[:1]}|{at_ct}"
    prior = book.get(key)
    if isinstance(prior, str) and prior:
        return prior
    if not active:
        return None
    moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).isoformat()
    book[key] = stamp
    return stamp


def commit_day_store(
    store: dict[str, Any],
    path: Any,
    write: Callable[[Any, dict[str, Any]], None],
    *,
    day: str,
    symbols: list[str],
    detected: dict[str, str],
    active: bool,
) -> None:
    """Persist the day's names and detection clocks. No-op once they match."""
    if not active:
        return
    fresh = {
        "day": day,
        "symbols": list(symbols),
        "detected": {str(key): str(value) for key, value in detected.items() if value},
    }
    current = {
        "day": store.get("day"),
        "symbols": list(store.get("symbols") or []) if store.get("day") == day else None,
        "detected": dict(store.get("detected") or {}) if store.get("day") == day else None,
    }
    if fresh == current:
        return
    write(path, fresh)
    store["day"] = fresh["day"]
    store["symbols"] = list(fresh["symbols"])
    store["detected"] = dict(fresh["detected"])
