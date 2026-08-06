from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable


WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def calendar_sets(cfg: dict[str, Any]) -> dict[str, set[str]]:
    cals = cfg.get("expiry_calendars") or {}
    return {
        "everyday": set(cals.get("everyday") or []),
        "monday_wednesday": set(cals.get("monday_wednesday") or []),
        "wednesday_only": set(cals.get("wednesday_only") or []),
        "friday_weeklies": set(cals.get("friday_weeklies") or []),
    }


def resolve_yahoo_symbol(symbol: str, cfg: dict[str, Any] | None = None) -> str:
    """Map display tickers (SPX) to Yahoo symbols (^SPX)."""
    sym = str(symbol).upper()
    aliases = (cfg or {}).get("symbol_aliases") or {}
    return str(aliases.get(sym, aliases.get(symbol, symbol)))


def resolve_universe(cfg: dict[str, Any]) -> list[str]:
    """Deduped ticker list from config.tickers, falling back to calendar unions."""
    seen: set[str] = set()
    out: list[str] = []
    for sym in cfg.get("tickers") or []:
        s = str(sym).upper().lstrip("^")
        # Keep SPX/XSP as clean display symbols
        if s not in seen:
            seen.add(s)
            out.append(s)
    if out:
        return out
    for group in calendar_sets(cfg).values():
        for s in sorted(group):
            s = s.upper().lstrip("^")
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def expiry_tags(symbol: str, cfg: dict[str, Any]) -> list[str]:
    sets = calendar_sets(cfg)
    sym = symbol.upper().lstrip("^")
    tags: list[str] = []
    if sym in sets["everyday"]:
        tags.append("Everyday")
    if sym in sets["monday_wednesday"]:
        tags.append("Mon+Wed")
    if sym in sets["wednesday_only"]:
        tags.append("Wed-only")
    if sym in sets["friday_weeklies"]:
        tags.append("Fri-weekly")
    return tags or ["unclassified"]


def has_weekday_expiry(symbol: str, weekday: int, cfg: dict[str, Any]) -> bool:
    """weekday: Monday=0 … Friday=4. Fridays always allowed for listed names."""
    sets = calendar_sets(cfg)
    sym = symbol.upper().lstrip("^")
    if weekday == 4:  # Friday — never dropped
        return True
    if sym in sets["everyday"] and weekday in (0, 1, 2, 3, 4):
        return True
    if weekday == 0:
        return sym in sets["monday_wednesday"]
    if weekday == 2:
        return sym in sets["monday_wednesday"] or sym in sets["wednesday_only"]
    # Tue/Thu: everyday names (SPY/QQQ/IWM/SPX/XSP)
    if weekday in (1, 3):
        return sym in sets["everyday"]
    return False


def today_weekday(now: date | datetime | None = None) -> int:
    if now is None:
        now = datetime.now()
    if isinstance(now, datetime):
        return now.weekday()
    return now.weekday()


def mon_wed_priority_symbols(cfg: dict[str, Any], weekday: int | None = None) -> set[str]:
    wd = today_weekday() if weekday is None else weekday
    sets = calendar_sets(cfg)
    everyday = set(sets["everyday"])
    if wd in (0, 1, 3):  # Mon / Tue / Thu
        base = set(sets["monday_wednesday"]) | everyday
        if wd == 0:
            return base
        return everyday  # Tue/Thu: everyday 0DTE only
    if wd == 2:  # Wednesday
        return set(sets["monday_wednesday"]) | set(sets["wednesday_only"]) | everyday
    if wd == 4:  # Friday — full universe eligible
        return (
            set(sets["monday_wednesday"])
            | set(sets["wednesday_only"])
            | set(sets["friday_weeklies"])
            | everyday
        )
    return set()


def filter_for_session(
    symbols: Iterable[str],
    cfg: dict[str, Any],
    *,
    weekday: int | None = None,
) -> list[str]:
    """Optionally restrict candidate emission; Fridays keep full list."""
    wd = today_weekday() if weekday is None else weekday
    scan = cfg.get("scan") or {}
    if not scan.get("mon_wed_candidates_only"):
        return list(symbols)
    if wd == 4 or scan.get("include_friday_weeklies", True):
        # Never strip Friday weeklies from the session list when include flag is on
        if wd == 4:
            return list(symbols)
    if wd not in (0, 1, 2, 3):
        return list(symbols)
    allowed = mon_wed_priority_symbols(cfg, wd)
    return [s for s in symbols if s in allowed]
