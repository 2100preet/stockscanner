"""Underlying-move outcomes for a logged signal.

Horizons follow the signal's session, not the option premium:

- end of day: close of the signal session versus the reference underlying price
- 1 day / 5 days: close one and five trading sessions later
- max favorable / max adverse: session high and low from the signal session
  through the fifth later session

A positive number means the underlying moved in the signal's direction.
Figures are hypothetical model marks. They are not fills and not account profit.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.json_util import finite_float
from odte_scanner.time_cst import CT

ET = ZoneInfo("America/New_York")

HORIZONS = ("eod", "d1", "d5")


def _session_closed(session: date, now: datetime, *, has_later_bar: bool) -> bool:
    if has_later_bar:
        return True
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    close = datetime.combine(session, time(16, 0), tzinfo=ET)
    return now.astimezone(ET) >= close


def _signal_day(signaled_at: str) -> date | None:
    try:
        dt = datetime.fromisoformat(str(signaled_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CT).date()


def _dir_move(entry: float, mark: float, direction: str) -> float:
    raw = (mark - entry) / entry * 100.0
    if direction == "short":
        raw = -raw
    return round(raw, 4)


def _horizon(status: str, move: float | None, mark: float | None, session: str | None) -> dict[str, Any]:
    settled = status == "settled" and move is not None
    return {
        "status": status,
        "return_pct": move if settled else None,
        "win": (move > 0) if settled else None,
        "mark": mark if settled else None,
        "session": session if settled else None,
    }


def _empty_pending() -> dict[str, Any]:
    return {
        "eod": _horizon("pending", None, None, None),
        "d1": _horizon("pending", None, None, None),
        "d5": _horizon("pending", None, None, None),
        "mfe_pct": None,
        "mae_pct": None,
        "mfe_status": "pending",
    }


def evaluate_outcome(
    signal: dict[str, Any],
    bars: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score one signal against daily bars. Missing data stays pending."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    base = _empty_pending()
    entry = finite_float(signal.get("reference_entry"))
    direction = str(signal.get("direction") or "long")
    if entry is None or entry <= 0 or direction not in {"long", "short"}:
        return base
    signal_day = _signal_day(str(signal.get("signaled_at") or ""))
    if signal_day is None:
        return base

    parsed: list[tuple[date, dict[str, Any]]] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        try:
            session = date.fromisoformat(str(bar.get("date"))[:10])
        except ValueError:
            continue
        close = finite_float(bar.get("close"))
        if close is None:
            continue
        parsed.append((session, bar))
    parsed.sort(key=lambda item: item[0])
    if not parsed:
        return base

    by_date = {session: bar for session, bar in parsed}
    dates = [session for session, _ in parsed]
    if signal_day in by_date:
        anchor = signal_day
    else:
        later = [session for session in dates if session > signal_day]
        if not later:
            return base
        anchor = later[0]

    def settled_close(session: date) -> dict[str, Any]:
        bar = by_date.get(session)
        if bar is None:
            return _horizon("pending", None, None, None)
        close = finite_float(bar.get("close"))
        has_later = any(item > session for item in dates)
        if close is None or not _session_closed(session, moment, has_later_bar=has_later):
            return _horizon("pending", None, None, None)
        move = _dir_move(entry, close, direction)
        return _horizon("settled", move, round(close, 6), session.isoformat())

    following = [session for session in dates if session > anchor]
    outcome = {
        "eod": settled_close(anchor),
        "d1": settled_close(following[0]) if following else _horizon("pending", None, None, None),
        "d5": settled_close(following[4]) if len(following) >= 5 else _horizon("pending", None, None, None),
        "mfe_pct": None,
        "mae_pct": None,
        "mfe_status": "pending",
    }
    # Excursions start once the signal session itself has closed, so a live
    # bar cannot publish a result before the horizon is done.
    if outcome["eod"]["status"] != "settled":
        return outcome

    end = following[4] if len(following) >= 5 else dates[-1]
    window = [by_date[session] for session in dates if anchor <= session <= end]
    fav: list[float] = []
    adv: list[float] = []
    for bar in window:
        high = finite_float(bar.get("high"))
        low = finite_float(bar.get("low"))
        if high is None or low is None:
            close = finite_float(bar.get("close"))
            high = high if high is not None else close
            low = low if low is not None else close
        if high is None or low is None:
            continue
        if direction == "short":
            fav.append(_dir_move(entry, low, direction))
            adv.append(_dir_move(entry, high, direction))
        else:
            fav.append(_dir_move(entry, high, direction))
            adv.append(_dir_move(entry, low, direction))
    if fav:
        outcome["mfe_pct"] = round(max(fav), 4)
        outcome["mae_pct"] = round(min(adv), 4)
        outcome["mfe_status"] = "settled" if outcome["d5"]["status"] == "settled" else "partial"
    return outcome


def outcome_fingerprint(outcome: dict[str, Any]) -> str:
    """Identity of a result, ignoring timestamps, so identical marks are not re-logged."""
    from odte_scanner.scorecard.ledger import canonical

    kept = {
        "eod": outcome.get("eod"),
        "d1": outcome.get("d1"),
        "d5": outcome.get("d5"),
        "mfe_pct": outcome.get("mfe_pct"),
        "mae_pct": outcome.get("mae_pct"),
        "mfe_status": outcome.get("mfe_status"),
    }
    return canonical(kept)
