"""Load public ledger signals, paper fills, and Yahoo underlying bars.

Read-only. This module does not import the broker and does not send orders.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.backtest.rule_replay import ROUTED, Bar, Idea

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

_OCC = re.compile(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})")
_SIGNAL_CLOCK = re.compile(r"signal\s+(\d{1,2}):(\d{2})\s+CT", re.I)

TIER_DESK = {
    "gold": "Gold Setup",
    "sniper": "Sniper",
    "diamond": "Diamond",
    "afternoon": "Afternoon Puts",
}


def parse_occ(contract: str | None) -> dict[str, Any] | None:
    raw = str(contract or "").upper().replace("O:", "").replace(" ", "")
    match = _OCC.search(raw)
    if not match:
        return None
    root, yymmdd, right, strike_raw = match.groups()
    expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    return {
        "symbol": root,
        "expiry": expiry,
        "right": right,
        "strike": int(strike_raw) / 1000.0,
        "contract": f"{root}{yymmdd}{right}{strike_raw}",
    }


def _aware(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ET)
    return moment


def _day_ct(moment: datetime) -> str:
    return moment.astimezone(CT).date().isoformat()


def _clock_on(day: str, hour: int, minute: int) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, hour, minute, tzinfo=CT)


def load_ledger_signals(path: Path, *, start: str, end: str) -> list[Idea]:
    ideas: list[Idea] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "signal":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            day = str(payload.get("day") or "")[:10]
            if not (start <= day <= end):
                continue
            sid = str(payload.get("id") or "")
            if sid and sid in seen:
                continue
            if sid:
                seen.add(sid)
            desk = str(payload.get("signal_type") or "Other")
            signaled = _aware(payload.get("signaled_at"))
            recorded = _aware(payload.get("recorded_at")) or signaled
            if signaled is None or recorded is None:
                continue
            try:
                ask = float(payload.get("premium_reference"))
                spot = float(payload.get("reference_entry") or 0)
            except (TypeError, ValueError):
                continue
            if ask <= 0:
                continue
            occ = parse_occ(payload.get("contract"))
            right = str(payload.get("right") or (occ or {}).get("right") or "C").upper()[:1]
            if right not in {"C", "P"}:
                right = "C" if str(payload.get("direction") or "").lower() != "short" else "P"
            symbol = str(payload.get("ticker") or (occ or {}).get("symbol") or "").upper()
            if not symbol or symbol in {"SPX", "XSP"}:
                desk = "SPX"
                symbol = symbol or "SPX"
            ideas.append(
                Idea(
                    desk=desk,
                    symbol=symbol,
                    right=right if right in {"C", "P"} else "C",
                    day=day,
                    signaled_at=signaled,
                    seen_at=recorded,
                    signal_ask=ask,
                    spot=spot,
                    contract=str((occ or {}).get("contract") or payload.get("contract") or ""),
                    quote_kind="ask" if str(payload.get("reference_basis") or "") != "structure" else "ask",
                    expiry=(occ or {}).get("expiry"),
                    strike=(occ or {}).get("strike"),
                    source="ledger",
                )
            )
    return ideas


def _signal_stamp(order: dict[str, Any], created: datetime) -> datetime:
    reason = str(order.get("reason") or "")
    match = _SIGNAL_CLOCK.search(reason)
    if not match:
        return created
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return created
    return _clock_on(_day_ct(created), hour, minute)


def load_snapshot_fills(path: Path, *, start: str, end: str) -> tuple[list[Idea], dict[tuple[str, str], float]]:
    """Filled buys, plus realized P&L keyed by (Chicago entry day, contract)."""
    blob = json.loads(path.read_text(encoding="utf-8"))
    book = blob.get("webull_book") if isinstance(blob, dict) else {}
    orders = book.get("orders") if isinstance(book, dict) else []
    trips = book.get("trips") if isinstance(book, dict) else []
    ideas: list[Idea] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("action") or "").upper() != "BUY":
            continue
        if str(order.get("status") or "").lower() != "filled":
            continue
        desk = TIER_DESK.get(str(order.get("tier") or "").lower())
        if desk is None:
            continue
        created = _aware(order.get("created_at"))
        filled_at = _aware(order.get("updated_at")) or created
        if created is None or filled_at is None:
            continue
        day = _day_ct(created)
        if not (start <= day <= end):
            continue
        try:
            fill = float(order.get("fill_price") or 0)
        except (TypeError, ValueError):
            fill = 0.0
        if fill <= 0:
            continue
        try:
            limit = float(order.get("limit_price") or 0)
        except (TypeError, ValueError):
            limit = 0.0
        occ = parse_occ(order.get("contract"))
        right = str(order.get("right") or (occ or {}).get("right") or "C").upper()[:1]
        symbol = str(order.get("symbol") or (occ or {}).get("symbol") or "").upper()
        if not symbol or symbol in {"SPX", "XSP"}:
            continue
        ideas.append(
            Idea(
                desk=desk,
                symbol=symbol,
                right=right if right in {"C", "P"} else "C",
                day=day,
                signaled_at=_signal_stamp(order, created),
                seen_at=created,
                signal_ask=limit if limit > 0 else fill,
                spot=0.0,
                contract=str((occ or {}).get("contract") or order.get("contract") or ""),
                quote_kind="mid",
                expiry=(occ or {}).get("expiry") if occ else _expiry_field(order.get("expiry")),
                strike=(occ or {}).get("strike") if occ else _float_or_none(order.get("strike")),
                actual_entry=fill,
                actual_entry_at=filled_at,
                source="order",
            )
        )
    pnl: dict[tuple[str, str], float] = {}
    for trip in trips or []:
        if not isinstance(trip, dict):
            continue
        entered = _aware(trip.get("entered_at"))
        if entered is None:
            continue
        day = _day_ct(entered)
        contract = str(trip.get("contract") or "")
        try:
            pnl[(day, contract)] = float(trip.get("pnl"))
        except (TypeError, ValueError):
            continue
    return ideas, pnl


def _expiry_field(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def merge_ideas(ledger: list[Idea], orders: list[Idea], pnl: dict[tuple[str, str], float]) -> list[Idea]:
    """One row per desk, day, and contract. A paper fill upgrades a ledger signal."""
    merged: dict[tuple[str, str, str], Idea] = {}
    for idea in ledger:
        key = (idea.desk, idea.day, idea.contract or f"{idea.symbol}:{idea.signaled_at.isoformat()}")
        merged[key] = idea
    for idea in orders:
        key = (idea.desk, idea.day, idea.contract or f"{idea.symbol}:{idea.signaled_at.isoformat()}")
        prior = merged.get(key)
        actual = pnl.get((idea.day, idea.contract))
        if prior is None:
            merged[key] = Idea(
                **{**idea.__dict__, "actual_pnl": actual},
            )
            continue
        merged[key] = Idea(
            desk=prior.desk,
            symbol=prior.symbol,
            right=prior.right or idea.right,
            day=prior.day,
            signaled_at=prior.signaled_at,
            seen_at=idea.seen_at,
            signal_ask=prior.signal_ask,
            spot=prior.spot or idea.spot,
            contract=prior.contract or idea.contract,
            quote_kind=prior.quote_kind,
            expiry=prior.expiry or idea.expiry,
            strike=prior.strike if prior.strike is not None else idea.strike,
            actual_entry=idea.actual_entry,
            actual_entry_at=idea.actual_entry_at,
            actual_pnl=actual if actual is not None else prior.actual_pnl,
            source="ledger+order",
        )
    for key, idea in list(merged.items()):
        actual = pnl.get((idea.day, idea.contract))
        if actual is not None and idea.actual_pnl is None:
            merged[key] = Idea(**{**idea.__dict__, "actual_pnl": actual})
    return list(merged.values())


def routed_symbols(ideas: list[Idea]) -> list[str]:
    found = {idea.symbol for idea in ideas if idea.desk in ROUTED and idea.symbol not in {"SPX", "XSP"}}
    return sorted(found)


def _frame_bars(frame: Any, symbol: str) -> list[Bar]:
    import pandas as pd

    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    if isinstance(frame.columns, pd.MultiIndex):
        if symbol in frame.columns.get_level_values(-1):
            sub = frame.xs(symbol, axis=1, level=-1)
        elif symbol in frame.columns.get_level_values(0):
            sub = frame.xs(symbol, axis=1, level=0)
        else:
            return []
    else:
        sub = frame
    bars: list[Bar] = []
    for ts, row in sub.iterrows():
        try:
            close = float(row["Close"])
            high = float(row["High"])
            low = float(row["Low"])
            opened = float(row["Open"])
        except (TypeError, ValueError, KeyError):
            continue
        if close != close or close <= 0:
            continue
        stamp = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if getattr(stamp, "tzinfo", None) is None:
            stamp = stamp.replace(tzinfo=ET)
        bars.append(Bar(ts=stamp, open=opened, high=high, low=low, close=close))
    bars.sort(key=lambda bar: bar.ts)
    return bars


def _download(symbols: list[str], start: str, end: str, interval: str) -> dict[str, list[Bar]]:
    import yfinance as yf

    frame = yf.download(
        symbols,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=False,
        progress=False,
        threads=False,
        group_by="ticker",
    )
    return {symbol: _frame_bars(frame, symbol) for symbol in symbols}


def _merge_resolution(minute: list[Bar], coarse: list[Bar]) -> list[Bar]:
    """Prefer 1-minute bars on days Yahoo still has them. Older days stay 2-minute."""
    minute_days = {bar.ts.astimezone(ET).date() for bar in minute}
    kept = list(minute)
    kept.extend(bar for bar in coarse if bar.ts.astimezone(ET).date() not in minute_days)
    kept.sort(key=lambda bar: bar.ts)
    return kept


def load_underlying_bars(symbols: list[str], *, start: str, end: str, cache: Path | None = None) -> dict[str, list[Bar]]:
    """Yahoo equity bars. One-minute where the vendor still has them, else two-minute.

    There is no options history here. Callers must keep the delta-proxy label.
    """
    if cache is not None and cache.exists():
        blob = json.loads(cache.read_text(encoding="utf-8"))
        return {
            symbol: [
                Bar(
                    ts=datetime.fromisoformat(row["ts"]),
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                )
                for row in rows
            ]
            for symbol, rows in blob.items()
        }
    # Yahoo publishes 1-minute bars for about seven days. Two-minute covers the rest.
    minute = _download(symbols, "2026-11-02", "2026-11-07", "1m") if end >= "2026-11-02" else {}
    coarse = _download(symbols, start, "2026-11-07", "2m")
    merged = {
        symbol: _merge_resolution(minute.get(symbol) or [], coarse.get(symbol) or [])
        for symbol in symbols
    }
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            symbol: [
                {
                    "ts": bar.ts.isoformat(),
                    "o": bar.open,
                    "h": bar.high,
                    "l": bar.low,
                    "c": bar.close,
                }
                for bar in bars
            ]
            for symbol, bars in merged.items()
        }
        cache.write_text(json.dumps(payload), encoding="utf-8")
    return merged
