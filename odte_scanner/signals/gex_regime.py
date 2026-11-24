"""Prior-day EOD GEX regime for SPY and QQQ from Bullflow REST ``eodGex``.

Net GEX summed over strikes within ±2% of the closing spot (all listed
expirations). Positive = dealers long gamma (dampened), negative = short
gamma (amplified). Fetched once per ET day (pre-market or first scan),
paced, cached on the Fly volume, and fail-open to regime ``unknown``.

REST only: one ``GET /v1/data/eodGex`` per symbol per day. This module never
touches the Bullflow SSE stream and never logs or stores the API key.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = ROOT / "outputs" / "gex_regime.json"
BASE_URL = "https://api.bullflow.io"
EOD_GEX_PATH = "/v1/data/eodGex"
SYMBOLS: tuple[str, ...] = ("SPY", "QQQ")
BAND = 0.02
PACE_SEC = 1.5
RETRY_AFTER_SEC = 900
MAX_ATTEMPTS_PER_DAY = 4
WARM_FROM_ET = (7, 0)

_lock = threading.RLock()
_worker_started = False


def cache_path() -> Path:
    raw = (os.environ.get("GEX_REGIME_CACHE_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_CACHE


def et_day(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ET).date().isoformat()


def regime_from_eod(payload: dict[str, Any], *, band: float = BAND) -> dict[str, Any]:
    """Sum ``net_gex`` for strikes within ±band of ``spot_price``."""
    try:
        spot = float(payload.get("spot_price"))
    except (TypeError, ValueError):
        return {"regime": "unknown", "error": "no spot_price"}
    if spot <= 0:
        return {"regime": "unknown", "error": "bad spot_price"}
    lo, hi = spot * (1 - band), spot * (1 + band)
    net = 0.0
    n = 0
    for row in payload.get("strikes") or []:
        try:
            k = float(row.get("strike"))
            g = float(row.get("net_gex") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if lo <= k <= hi:
            net += g
            n += 1
    if n == 0:
        return {"regime": "unknown", "spot": spot, "strikes_in_band": 0, "error": "no strikes in band"}
    regime = "positive" if net > 0 else "negative" if net < 0 else "unknown"
    return {
        "regime": regime,
        "net_gex_band": round(net, 2),
        "spot": spot,
        "band_pct": band * 100,
        "strikes_in_band": n,
        "session": payload.get("date"),
        "open_interest_date": payload.get("open_interest_date"),
        "as_of": payload.get("as_of"),
    }


def fetch_eod_gex(
    symbol: str,
    *,
    api_key: str,
    session: Any | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """One REST call. Errors come back as dicts; nothing here raises or logs the key."""
    from odte_scanner.signals.bullflow import _auth_headers, _safe_error_body

    client = session or requests
    try:
        resp = client.get(
            f"{BASE_URL}{EOD_GEX_PATH}",
            headers=_auth_headers(api_key),
            params={"ticker": symbol},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}
    status = getattr(resp, "status_code", 0)
    if status != 200:
        return {"ok": False, "status_code": status, "error": _safe_error_body(resp)}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "invalid JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "unexpected body"}
    return {"ok": True, "data": body}


def _read(path: Path) -> dict[str, Any]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write(path: Path, blob: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("gex regime cache not writable: %s", type(exc).__name__)


def _unknown(symbol: str, day: str, note: str) -> dict[str, Any]:
    return {"symbol": symbol, "day": day, "regime": "unknown", "note": note}


def regime_block(now: datetime | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Cached regime for today. Pure read: never fetches (safe in request paths)."""
    day = et_day(now)
    blob = _read(path or cache_path())
    out: dict[str, Any] = {"day": day, "band_pct": BAND * 100, "source": "bullflow eodGex", "symbols": {}}
    stored = blob.get("days", {}).get(day, {}) if isinstance(blob.get("days"), dict) else {}
    for sym in SYMBOLS:
        row = stored.get(sym) if isinstance(stored, dict) else None
        out["symbols"][sym] = dict(row) if isinstance(row, dict) else _unknown(sym, day, "not fetched yet")
    out["fetched"] = bool(stored)
    return out


def regime_for(symbol: str, now: datetime | None = None, *, path: Path | None = None) -> str:
    row = regime_block(now, path=path)["symbols"].get(str(symbol).upper())
    return str((row or {}).get("regime") or "unknown")


def ensure_today(
    now: datetime | None = None,
    *,
    path: Path | None = None,
    api_key: str | None = None,
    fetch: Callable[..., dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch today's regime once (paced, bounded retries). Fail open to unknown."""
    moment = now or datetime.now(timezone.utc)
    day = et_day(moment)
    p = path or cache_path()
    if api_key is None:
        from odte_scanner.signals.bullflow import api_key_from_env

        api_key = api_key_from_env()
    getter = fetch or fetch_eod_gex
    with _lock:
        blob = _read(p)
        days = blob.get("days") if isinstance(blob.get("days"), dict) else {}
        today = dict(days.get(day) or {})
        attempts = blob.get("attempts") if isinstance(blob.get("attempts"), dict) else {}
        tries = dict(attempts.get(day) or {"n": 0, "last": 0})
        done = all(isinstance(today.get(s), dict) and today[s].get("regime") in {"positive", "negative"} for s in SYMBOLS)
        if done:
            return regime_block(moment, path=p)
        now_ts = int(moment.timestamp())
        if tries["n"] >= MAX_ATTEMPTS_PER_DAY or (tries["n"] and now_ts - int(tries["last"]) < RETRY_AFTER_SEC):
            return regime_block(moment, path=p)
        tries = {"n": int(tries["n"]) + 1, "last": now_ts}
        for i, sym in enumerate(SYMBOLS):
            if isinstance(today.get(sym), dict) and today[sym].get("regime") in {"positive", "negative"}:
                continue
            if not api_key:
                today[sym] = _unknown(sym, day, "BULLFLOW_API_KEY not set")
                continue
            if i:
                sleep(PACE_SEC)
            res = getter(sym, api_key=api_key)
            if res.get("ok"):
                row = regime_from_eod(res.get("data") or {})
                row.update({"symbol": sym, "day": day, "fetched_at": moment.astimezone(timezone.utc).isoformat()})
            else:
                row = _unknown(sym, day, f"eodGex unavailable ({str(res.get('error') or 'error')[:60]})")
            today[sym] = row
        days = {k: v for k, v in days.items() if k >= (moment.astimezone(ET).date() - timedelta(days=45)).isoformat()}
        days[day] = today
        attempts = {day: tries}
        _write(p, {"version": 1, "days": days, "attempts": attempts})
    return regime_block(moment, path=p)


def _worker_loop() -> None:
    while True:
        try:
            moment = datetime.now(timezone.utc)
            et = moment.astimezone(ET)
            if et.weekday() < 5 and (et.hour, et.minute) >= WARM_FROM_ET:
                ensure_today(moment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gex regime warm failed: %s", type(exc).__name__)
        time.sleep(300)


def start_gex_regime_worker() -> bool:
    """Warm the cache pre-market (from 7:00 ET) or right after a deploy. REST only."""
    global _worker_started
    if (os.environ.get("SIGNAL_DESK_OFFLINE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if (os.environ.get("GEX_REGIME_ENABLED") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    with _lock:
        if _worker_started:
            return False
        _worker_started = True
    threading.Thread(target=_worker_loop, name="gex-regime-warm", daemon=True).start()
    return True
