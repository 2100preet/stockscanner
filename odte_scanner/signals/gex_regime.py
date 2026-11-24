"""Prior-day EOD net GEX regime for SPY and QQQ. Context and logging only.

Definition (matches the Nov 24, 2026 split-half backtest in
``bullflow_bt/pull_gex.py`` + ``gex_bt.py``): call Bullflow REST
``GET /v1/data/eodGex?ticker=T&date=<prior session>``, sum ``net_gex`` over
every expiration by strike, keep strikes strictly inside ±2% of the
response's ``spot_price``, and add them up. ``> 0`` is Positive GEX,
otherwise Negative GEX.

* REST only, one cached call per ticker per session day (a failed call is
  retried at most ``MAX_ATTEMPTS`` times, ``RETRY_SEC`` apart). This module
  never opens the Bullflow SSE stream: Golden Flow owns the single allowed
  connection.
* Fail open: any error leaves the regime ``unknown``. Nothing here routes,
  sizes, or blocks an order.
* ``BULLFLOW_API_KEY`` goes in the X-API-Key header only. It is never logged,
  printed, or persisted.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "gex_regime.json"
EOD_GEX_PATH = "/v1/data/eodGex"
TICKERS: tuple[str, ...] = ("SPY", "QQQ")
BAND_PCT = 0.02
MAX_ATTEMPTS = 3
RETRY_SEC = 30 * 60
FETCH_FROM_CT = time(6, 0)
KEEP_DAYS = 10

POSITIVE = "positive"
NEGATIVE = "negative"
UNKNOWN = "unknown"
LABELS = {POSITIVE: "Positive GEX", NEGATIVE: "Negative GEX", UNKNOWN: "GEX unknown"}

# SPX is alert only and has no own eodGex in the backtest. It borrows SPY.
PROXY = {"SPX": "SPY"}

_lock = threading.Lock()


def is_session(day: date) -> bool:
    try:
        from odte_scanner.market.levels import MARKET_HOLIDAYS
    except Exception:  # noqa: BLE001
        MARKET_HOLIDAYS = frozenset()  # type: ignore[assignment]
    return day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS


def prior_session(day: date) -> date:
    """The last session strictly before ``day``."""
    prev = day - timedelta(days=1)
    for _ in range(10):
        if is_session(prev):
            return prev
        prev -= timedelta(days=1)
    return prev


def net_gex_band(body: dict[str, Any] | None, band: float = BAND_PCT) -> dict[str, Any] | None:
    """Net GEX within ±band of spot, summed over all expirations. None when unusable."""
    if not isinstance(body, dict):
        return None
    try:
        spot = float(body.get("spot_price"))
    except (TypeError, ValueError):
        return None
    rows = body.get("strikes")
    if spot <= 0 or not isinstance(rows, list) or not rows:
        return None
    by_strike: dict[float, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            strike = float(row.get("strike"))
            net = float(row.get("net_gex") or 0.0)
        except (TypeError, ValueError):
            continue
        by_strike[strike] = by_strike.get(strike, 0.0) + net
    lo, hi = spot * (1 - band), spot * (1 + band)
    inside = [k for k in by_strike if lo < k < hi]
    if not inside:
        return None
    total = sum(by_strike[k] for k in inside)
    return {"spot": spot, "net_gex": total, "strikes_in_band": len(inside)}


def classify(net: float | None) -> str:
    if net is None:
        return UNKNOWN
    return POSITIVE if net > 0 else NEGATIVE


def fetch_eod_gex(
    ticker: str,
    gex_date: str,
    *,
    session: Any = None,
    timeout: float = 20.0,
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """(status, body, error). REST only. The key never leaves the header."""
    from odte_scanner.signals.bullflow import BASE_URL, _auth_headers, api_key_from_env

    key = api_key_from_env()
    if not key:
        return None, None, "BULLFLOW_API_KEY not set"
    import requests

    client = session or requests
    try:
        resp = client.get(
            f"{BASE_URL}{EOD_GEX_PATH}",
            params={"ticker": ticker, "date": gex_date},
            headers=_auth_headers(key),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return None, None, type(exc).__name__
    status = getattr(resp, "status_code", None)
    if status != 200:
        return status, None, f"HTTP {status}"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return status, None, "bad json"
    return status, body if isinstance(body, dict) else None, None


def _read(path: Path) -> dict[str, Any]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _write(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(blob), encoding="utf-8")
    tmp.replace(path)


def _ct(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _unknown_row(ticker: str, gex_date: str, error: str | None = None) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "gex_date": gex_date,
        "regime": UNKNOWN,
        "label": LABELS[UNKNOWN],
        "net_gex": None,
        "spot": None,
        "error": error,
        "attempts": 0,
        "last_attempt": None,
    }


def ensure_regimes(
    now: datetime | None = None,
    *,
    store_path: Path | None = None,
    fetch: Callable[[str, str], tuple[int | None, dict[str, Any] | None, str | None]] | None = None,
) -> dict[str, Any]:
    """Fetch today's regimes once (per ticker). Cached on disk. Never raises."""
    try:
        return _ensure(now, store_path=store_path, fetch=fetch)
    except Exception as exc:  # noqa: BLE001
        logger.info("gex regime refresh failed: %s", type(exc).__name__)
        return cached_block(now, store_path=store_path)


def _ensure(now, *, store_path, fetch) -> dict[str, Any]:
    now_ct = _ct(now)
    day = now_ct.date()
    path = store_path or DEFAULT_STORE
    if not is_session(day) or now_ct.time() < FETCH_FROM_CT:
        return cached_block(now, store_path=path)
    gex_date = prior_session(day).isoformat()
    getter = fetch or (lambda t, d: fetch_eod_gex(t, d))
    with _lock:
        store = _read(path)
        days = store.get("days") if isinstance(store.get("days"), dict) else {}
        today = days.get(day.isoformat()) if isinstance(days.get(day.isoformat()), dict) else {}
        changed = False
        for ticker in TICKERS:
            row = today.get(ticker) if isinstance(today.get(ticker), dict) else _unknown_row(ticker, gex_date)
            if row.get("regime") in (POSITIVE, NEGATIVE):
                continue
            attempts = int(row.get("attempts") or 0)
            if attempts >= MAX_ATTEMPTS:
                continue
            last = row.get("last_attempt")
            if last:
                try:
                    if (now_ct - datetime.fromisoformat(last)).total_seconds() < RETRY_SEC:
                        continue
                except ValueError:
                    pass
            status, body, error = getter(ticker, gex_date)
            band = net_gex_band(body) if body is not None else None
            if band is None and error is None:
                error = "no strikes" if status == 200 else f"HTTP {status}"
            row = _unknown_row(ticker, gex_date, error if band is None else None)
            row["attempts"] = attempts + 1
            row["last_attempt"] = now_ct.isoformat()
            if band is not None:
                regime = classify(band["net_gex"])
                row.update(
                    regime=regime,
                    label=LABELS[regime],
                    net_gex=round(band["net_gex"], 2),
                    spot=band["spot"],
                    strikes_in_band=band["strikes_in_band"],
                    as_of=(body or {}).get("as_of"),
                    fetched_at=now_ct.isoformat(),
                )
            else:
                logger.info("gex regime %s %s unknown: %s", ticker, gex_date, row.get("error"))
            today[ticker] = row
            changed = True
        if changed:
            days[day.isoformat()] = today
            keep = sorted(days)[-KEEP_DAYS:]
            store = {"days": {k: days[k] for k in keep}}
            _write(path, store)
    return cached_block(now, store_path=path)


def cached_block(now: datetime | None = None, *, store_path: Path | None = None) -> dict[str, Any]:
    """Today's regimes from disk. No network. Missing tickers read as unknown."""
    now_ct = _ct(now)
    day = now_ct.date()
    gex_date = prior_session(day).isoformat()
    store = _read(store_path or DEFAULT_STORE)
    days = store.get("days") if isinstance(store.get("days"), dict) else {}
    today = days.get(day.isoformat()) if isinstance(days.get(day.isoformat()), dict) else {}
    tickers: dict[str, Any] = {}
    for ticker in TICKERS:
        row = today.get(ticker) if isinstance(today.get(ticker), dict) else _unknown_row(ticker, gex_date)
        public = {k: row.get(k) for k in ("ticker", "gex_date", "regime", "label", "net_gex", "spot", "strikes_in_band", "as_of", "error")}
        tickers[ticker] = public
    return {
        "day": day.isoformat(),
        "gex_date": gex_date,
        "source": "bullflow eodGex (REST, prior session)",
        "definition": "net GEX summed over strikes within ±2% of spot, all expirations; >0 Positive GEX",
        "context_only": True,
        "tickers": tickers,
    }


def regime_for(symbol: str, block: dict[str, Any] | None) -> dict[str, Any]:
    """Regime row for a desk symbol. SPX borrows SPY."""
    sym = str(symbol or "").upper()
    src = PROXY.get(sym, sym)
    rows = (block or {}).get("tickers") or {}
    row = rows.get(src) if isinstance(rows.get(src), dict) else None
    if not row:
        return {"regime": UNKNOWN, "label": LABELS[UNKNOWN], "source_ticker": src, "net_gex": None, "gex_date": (block or {}).get("gex_date")}
    out = {
        "regime": row.get("regime") or UNKNOWN,
        "label": row.get("label") or LABELS[UNKNOWN],
        "source_ticker": src,
        "net_gex": row.get("net_gex"),
        "gex_date": row.get("gex_date"),
    }
    if src != sym:
        out["proxy"] = True
    return out


def format_net(net: Any) -> str:
    try:
        value = float(net)
    except (TypeError, ValueError):
        return "—"
    sign = "−" if value < 0 else "+"
    value = abs(value)
    if value >= 1e9:
        return f"{sign}{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{sign}{value / 1e6:.1f}M"
    return f"{sign}{value:,.0f}"


def attach_to_lunch_board(board: dict[str, Any] | None, now: datetime | None = None, *, store_path: Path | None = None) -> dict[str, Any] | None:
    """Stamp the cached regime on the board and its cards. No network, never raises."""
    if not isinstance(board, dict):
        return board
    try:
        block = cached_block(now, store_path=store_path)
        board["gex_regime"] = block
        for card in board.get("signals") or []:
            if isinstance(card, dict):
                info = regime_for(str(card.get("symbol") or ""), block)
                card["gex_regime"] = info["regime"]
                card["gex_regime_label"] = info["label"]
                card["gex_regime_ticker"] = info["source_ticker"]
                card["gex_net"] = info.get("net_gex")
    except Exception as exc:  # noqa: BLE001
        logger.info("gex regime attach failed: %s", type(exc).__name__)
    return board


def lunch_alert_line(card: dict[str, Any]) -> str | None:
    regime = str(card.get("gex_regime") or "")
    if not regime:
        return None
    label = card.get("gex_regime_label") or LABELS.get(regime, LABELS[UNKNOWN])
    src = str(card.get("gex_regime_ticker") or card.get("symbol") or "").upper()
    sym = str(card.get("symbol") or "").upper()
    via = f" via {src}" if src and src != sym else ""
    if regime == UNKNOWN:
        return f"Prior-day GEX: unknown{via} · context only"
    return f"Prior-day GEX: {label}{via} (±2% net {format_net(card.get('gex_net'))}) · context only"
