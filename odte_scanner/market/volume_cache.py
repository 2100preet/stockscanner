"""Daily volume for the liquid universe, separate from the focus scan.

The focus scan only downloads history for its own names. This cache covers
the rest of the liquid board so a non-focus name can still rank by volume.
Tradier quotes are batched, the file is reused for the session day, and a
failed request leaves the last good rows in place. The warm runs on a
background thread so the scan cycle does not wait on it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.data.universe import liquid_universe
from odte_scanner.json_util import atomic_write_text

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "outputs" / "market_volume_cache.json"

_lock = threading.Lock()
_inflight: set[str] = set()


def session_day(now: datetime | None = None) -> str:
    moment = now or datetime.now(CT)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CT)
    return moment.astimezone(CT).date().isoformat()


def _empty(day: str) -> dict[str, Any]:
    return {"day": day, "symbols": {}}


def read_volume_cache(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Today's volume rows. A missing or older file is an empty map."""
    day = session_day(now)
    target = Path(path or CACHE_PATH)
    if not target.exists():
        return _empty(day)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty(day)
    if not isinstance(raw, dict) or raw.get("day") != day:
        return _empty(day)
    symbols = raw.get("symbols") if isinstance(raw.get("symbols"), dict) else {}
    return {"day": day, "symbols": {str(k).upper(): v for k, v in symbols.items() if isinstance(v, dict)}}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _from_quote(row: Mapping[str, Any]) -> dict[str, Any] | None:
    day_vol = _num(row.get("volume"))
    if day_vol is None or day_vol <= 0:
        return None
    avg = _num(row.get("average_volume"))
    avg_i = int(avg) if avg is not None and avg > 0 else None
    last = _num(row.get("last"))
    change = _num(row.get("change_percentage"))
    if change is None and last is not None:
        prev = _num(row.get("prevclose"))
        if prev is not None and prev > 0:
            change = round((last / prev - 1.0) * 100.0, 2)
    return {
        "day_volume": int(day_vol),
        "avg_volume_20": avg_i,
        "rel_volume": round(day_vol / avg_i, 2) if avg_i else None,
        "last": last,
        "change_pct": None if change is None else round(change, 2),
        "volume_source": "tradier",
    }


def _tradier_quotes(symbols: list[str]) -> dict[str, Any]:
    from odte_scanner.data.tradier import fetch_quotes

    return fetch_quotes(symbols, greeks=False)


def warm_liquid_volume_cache(
    *,
    symbols: list[str] | None = None,
    now: datetime | None = None,
    fetch: Callable[[list[str]], Mapping[str, Any]] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Fill today's cache for names that are not already stored.

    ``fetch`` receives the missing symbols in one call. Tradier's quote
    client splits that list into batches. A raised or empty failure keeps
    the previous rows.
    """
    names = [str(s).replace(".", "-").upper() for s in (symbols if symbols is not None else liquid_universe())]
    names = [s for s in dict.fromkeys(names) if s]
    day = session_day(now)
    target = Path(path or CACHE_PATH)
    current = read_volume_cache(path=target, now=now)
    have = dict(current.get("symbols") or {})
    missing = [s for s in names if s not in have]
    if not missing:
        return current
    fetch_fn = fetch or _tradier_quotes
    try:
        payload = fetch_fn(missing)
    except Exception as exc:  # noqa: BLE001
        logger.warning("liquid volume warm failed: %s", exc)
        return current
    if not isinstance(payload, Mapping):
        return current
    quotes = payload.get("quotes") if isinstance(payload.get("quotes"), Mapping) else {}
    if not quotes:
        if payload.get("ok") is False:
            logger.info("liquid volume warm skipped: %s", payload.get("error") or "no quotes")
        return current
    merged = dict(have)
    for sym, row in quotes.items():
        if not isinstance(row, Mapping):
            continue
        shaped = _from_quote(row)
        if shaped:
            merged[str(sym).upper()] = shaped
    body = {"day": day, "symbols": merged, "source": "tradier"}
    if payload.get("ok") is False and payload.get("error"):
        body["error"] = str(payload.get("error"))[:200]
    try:
        atomic_write_text(target, json.dumps(body))
    except Exception as exc:  # noqa: BLE001
        logger.warning("liquid volume cache write failed: %s", exc)
        return {"day": day, "symbols": merged}
    return body


def schedule_liquid_volume_warm(*, path: Path | None = None) -> bool:
    """Start one background warm for today. Returns immediately."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    day = session_day()
    cached = read_volume_cache(path=path)
    missing = [s for s in liquid_universe() if s not in (cached.get("symbols") or {})]
    if not missing:
        return False
    with _lock:
        if day in _inflight:
            return False
        _inflight.add(day)

    def _run() -> None:
        try:
            warm_liquid_volume_cache(path=path)
        finally:
            with _lock:
                _inflight.discard(day)

    threading.Thread(target=_run, name="market-volume-warm", daemon=True).start()
    return True
