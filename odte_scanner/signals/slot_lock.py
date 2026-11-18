"""Daily name lock for Sniper and Diamond.

A name keeps its slot once it is locked, so a later qualifier cannot open a
fourth seat (Nov 10 bought five Diamond names after earlier ones dropped).

A higher-priority name at the same or earlier bar can still take a seat that
was filled only because its own bars had not arrived. That is the Nov 17
Sniper miss: SPY, SPX, and NVDA locked at 9:15 CT while QQQ's 9:10 put bar
was still missing; QQQ later qualified but the cap was already full. SPX is
alert-only and is not a Webull order, but it does count as one of the three
names. The bug is the premature NVDA lock, not SPX occupying a seat.

This module does not change the cap, the universe, or the entry rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from odte_scanner.signals.diamond import DAILY_CAP, PRIORITY, UNIVERSE, Bar
from odte_scanner.time_cst import CT

# Wait through one extra closed 5-minute bar for a missing higher-priority name.
# A 9:10 CT signal's bar is closed at 9:15; we hold the leftover seats until 9:20.
HOLD_MINUTES = 10


def _symbol(row: Mapping[str, Any] | str) -> str:
    if isinstance(row, str):
        return row.upper()
    return str(row.get("symbol") or "").upper()


def _at_text(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    raw = str(row.get("at_ct") or "").strip()
    if raw:
        return raw
    at = row.get("at")
    if not at:
        return ""
    try:
        moment = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    return moment.astimezone(CT).strftime("%H:%M")


def parse_at_ct(day: str, at_ct: str) -> datetime | None:
    text = str(at_ct or "").strip()
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    try:
        year, month, dd = (int(p) for p in day.split("-"))
        return datetime(year, month, dd, int(parts[0]), int(parts[1]), tzinfo=CT)
    except ValueError:
        return None


def data_ready_at(bars: Sequence[Bar] | None, day: str, at_ct: str) -> bool:
    """True when this symbol already has a closed bar at or after the signal."""
    target = parse_at_ct(day, at_ct)
    if target is None or not bars:
        return False
    for bar in bars:
        stamp = bar.ct()
        if stamp.date().isoformat() == day and stamp >= target:
            return True
    return False


def _sent_symbols(store: Mapping[str, Any], day: str) -> set[str]:
    if store.get("day") != day or not isinstance(store.get("detected"), dict):
        return set()
    out: set[str] = set()
    for key in store["detected"]:
        if not store["detected"].get(key):
            continue
        sym = str(key).split("|", 1)[0].upper()
        if sym:
            out.add(sym)
    return out


def _rank(symbol: str, priority: Sequence[str]) -> int:
    try:
        return list(priority).index(symbol)
    except ValueError:
        return 99


def _claim(row: Mapping[str, Any], priority: Sequence[str]) -> tuple[str, int]:
    return (_at_text(row) or "99:99", _rank(_symbol(row), priority))


def _higher_pending(
    symbol: str,
    at_ct: str,
    *,
    day: str,
    now: datetime | None,
    bars_by_symbol: Mapping[str, Sequence[Bar]] | None,
    qualified: set[str],
    priority: Sequence[str],
    universe: Sequence[str],
) -> bool:
    """True when a better name might still print on this same bar."""
    if not at_ct or bars_by_symbol is None:
        return False
    target = parse_at_ct(day, at_ct)
    if target is None:
        return False
    moment = now
    if moment is not None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CT)
        if moment >= target + timedelta(minutes=HOLD_MINUTES):
            return False
    ahead = [
        name
        for name in priority
        if _rank(name, priority) < _rank(symbol, priority) and name in set(universe)
    ]
    for name in ahead:
        if name in qualified:
            continue
        if data_ready_at(bars_by_symbol.get(name), day, at_ct):
            continue
        return True
    return False


def lock_daily_symbols(
    selected: list[dict[str, Any]],
    store: dict[str, Any],
    day: str,
    *,
    bars_by_symbol: Mapping[str, Sequence[Bar]] | None = None,
    now: datetime | None = None,
    priority: Sequence[str] | None = None,
    universe: Sequence[str] | None = None,
    limit: int = DAILY_CAP,
) -> list[str]:
    """Keep locked names, then fill empty seats in signal order.

    When ``bars_by_symbol`` and ``now`` are provided, a candidate does not
    take a leftover seat while a higher-priority name still has no closed bar
    at that signal time. A better claim at the same or earlier bar may evict a
    locked name that has not been sent yet. A name that already has a
    detected stamp is never evicted.
    """
    order = tuple(priority or PRIORITY)
    names = tuple(universe or UNIVERSE)
    prior = [str(sym).upper() for sym in (store.get("symbols") or [])] if store.get("day") == day else []
    rows = [row for row in selected if _symbol(row)]
    by_symbol = { _symbol(row): row for row in rows }
    qualified = set(by_symbol)
    # Stable sort: signal time, then published priority, then current list order.
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (_at_text(item[1]) or "99:99", _rank(_symbol(item[1]), order), item[0]))
    ordered_rows = [item[1] for item in indexed]

    sent = _sent_symbols(store, day)
    kept = list(dict.fromkeys(prior))[: max(0, limit)]

    def can_add(row: Mapping[str, Any]) -> bool:
        return not _higher_pending(
            _symbol(row),
            _at_text(row),
            day=day,
            now=now,
            bars_by_symbol=bars_by_symbol,
            qualified=qualified,
            priority=order,
            universe=names,
        )

    for row in ordered_rows:
        sym = _symbol(row)
        if sym in kept:
            continue
        if len(kept) >= limit:
            break
        if can_add(row):
            kept.append(sym)
    kept = kept[:limit]

    # Evict a not-yet-sent worse claim so a late higher-priority bar can sit.
    # Once any locked name has already been sent, leave the trio alone: we
    # cannot unsend NVDA, and swapping out an unsent SPX would add a fourth
    # Webull name.
    if bars_by_symbol is not None and not (sent & set(kept) and len(kept) >= limit):
        for row in ordered_rows:
            sym = _symbol(row)
            if sym in kept or not can_add(row):
                continue
            evictable = [name for name in kept if name not in sent]
            if not evictable:
                continue
            # Prefer evicting the worst claim we can still describe.
            def evict_key(name: str) -> tuple[str, int]:
                other = by_symbol.get(name)
                if other is None:
                    # Unknown signal time: treat as already earlier so we do not
                    # evict Nov 10's locked names that no longer qualify.
                    return ("00:00", -1)
                return _claim(other, order)

            worst = max(evictable, key=evict_key)
            other = by_symbol.get(worst)
            if other is None:
                continue
            if _claim(row, order) < _claim(other, order):
                kept[kept.index(worst)] = sym

    def final_key(item: tuple[int, str]) -> tuple[str, int, int]:
        idx, name = item
        other = by_symbol.get(name)
        if other is None:
            return ("00:00", -1, idx)
        at_ct, rank = _claim(other, order)
        return (at_ct, rank, idx)

    kept = [name for _, name in sorted(enumerate(kept), key=final_key)]
    return kept[:limit]


def lock_symbols(
    selected: list[dict[str, Any]],
    store: dict[str, Any],
    day: str,
    **kwargs: Any,
) -> list[str]:
    """Backward-compatible alias used by the board modules and tests."""
    return lock_daily_symbols(selected, store, day, **kwargs)
