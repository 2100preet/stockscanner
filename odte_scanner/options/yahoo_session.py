"""Yahoo Finance session helpers (crumb cookie) for options + quotes.

yfinance often hits 429 rate limits; the crumb+query2 endpoints are more reliable
for challenge live option prices when a crumb can be obtained.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CHAIN_CACHE = ROOT / "outputs" / "option_chain_cache"
CRUMB_PATH = ROOT / "outputs" / "yahoo_crumb.json"
_LOCK = threading.Lock()
_SESSION: requests.Session | None = None
_CRUMB: str | None = None
_CRUMB_AT: float = 0.0


def _load_persisted_crumb() -> tuple[str, float] | None:
    if not CRUMB_PATH.exists():
        return None
    try:
        raw = json.loads(CRUMB_PATH.read_text())
        crumb = str(raw.get("crumb") or "")
        at = float(raw.get("at") or 0)
        if crumb and time.time() - at < 3600:
            return crumb, at
    except Exception:  # noqa: BLE001
        return None
    return None


def _persist_crumb(crumb: str) -> None:
    try:
        CRUMB_PATH.parent.mkdir(parents=True, exist_ok=True)
        CRUMB_PATH.write_text(json.dumps({"crumb": crumb, "at": time.time()}))
    except Exception:  # noqa: BLE001
        pass


def _session(force: bool = False) -> tuple[requests.Session, str] | None:
    global _SESSION, _CRUMB, _CRUMB_AT
    with _LOCK:
        now = time.time()
        if not force and _SESSION is not None and _CRUMB and now - _CRUMB_AT < 1800:
            return _SESSION, _CRUMB
        if not force:
            persisted = _load_persisted_crumb()
            if persisted and _SESSION is not None:
                _CRUMB, _CRUMB_AT = persisted
                return _SESSION, _CRUMB

        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        crumb = None
        for attempt in range(3):
            try:
                # A1/A3 cookies required — visit Yahoo home + options page first
                s.get("https://finance.yahoo.com/", timeout=12)
                s.get("https://finance.yahoo.com/quote/SPY/options", timeout=12)
                r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
                if r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                crumb = r.text.strip()
                if crumb and "Too Many" not in crumb and "<" not in crumb and "error" not in crumb.lower():
                    break
                crumb = None
            except Exception as exc:  # noqa: BLE001
                logger.debug("crumb attempt %s: %s", attempt, exc)
                time.sleep(1.0 * (attempt + 1))
        if not crumb:
            # Reuse persisted crumb with a fresh session cookie jar if possible
            persisted = _load_persisted_crumb()
            if persisted:
                crumb, _CRUMB_AT = persisted
                _SESSION = s
                _CRUMB = crumb
                return _SESSION, _CRUMB
            return None
        _SESSION = s
        _CRUMB = crumb
        _CRUMB_AT = now
        _persist_crumb(crumb)
        return _SESSION, _CRUMB


def fetch_yahoo_quote(symbol: str) -> dict[str, Any] | None:
    """Live / delayed underlying quote via chart API (no crumb required)."""
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0"})
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1m&range=1d&includePrePost=true"
        )
        r = s.get(url, timeout=15)
        if r.status_code == 429:
            # Daily chart often still works
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
            r = s.get(url, timeout=15)
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return None
        meta = res[0].get("meta") or {}
        last = (
            meta.get("regularMarketPrice")
            or meta.get("postMarketPrice")
            or meta.get("preMarketPrice")
        )
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if last is None:
            # try last close from timestamps
            closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            closes = [c for c in closes if c is not None]
            if closes:
                last = closes[-1]
        if last is None:
            return None
        last_f = float(last)
        prev_f = float(prev) if prev else last_f
        chg = last_f - prev_f
        return {
            "symbol": symbol.upper(),
            "last": last_f,
            "prev_close": prev_f,
            "change": chg,
            "change_pct": (chg / prev_f * 100.0) if prev_f else 0.0,
            "session": "regular",
            "asof": datetime.now(timezone.utc).isoformat(),
            "day_high": meta.get("regularMarketDayHigh"),
            "day_low": meta.get("regularMarketDayLow"),
            "source": "yahoo_chart",
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("yahoo chart quote %s: %s", symbol, exc)
        return None


def _cache_path(symbol: str, exp_ts: int | None) -> Path:
    CHAIN_CACHE.mkdir(parents=True, exist_ok=True)
    key = f"{symbol.upper()}_{exp_ts or 'nearest'}.json"
    return CHAIN_CACHE / key


def fetch_option_chain(
    symbol: str,
    *,
    expiry: str | None = None,
    yahoo_symbol: str | None = None,
    use_cache: bool = True,
    max_age_minutes: float = 45.0,
) -> dict[str, Any] | None:
    """Return Yahoo option chain payload for a symbol (optionally one expiry)."""
    fetch_sym = yahoo_symbol or symbol
    exp_ts: int | None = None
    if expiry:
        try:
            exp_ts = int(datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        except Exception:  # noqa: BLE001
            exp_ts = None

    path = _cache_path(fetch_sym, exp_ts)
    if use_cache and path.exists():
        age_m = (time.time() - path.stat().st_mtime) / 60.0
        if age_m <= max_age_minutes:
            try:
                return json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                pass

    sess = _session()
    if not sess:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None
    s, crumb = sess

    try:
        url = f"https://query2.finance.yahoo.com/v7/finance/options/{fetch_sym}?crumb={crumb}"
        if exp_ts:
            url += f"&date={exp_ts}"
        r = s.get(url, timeout=20)
        if r.status_code in {401, 403, 429}:
            # refresh crumb once
            time.sleep(1.0)
            sess2 = _session(force=True)
            if not sess2:
                raise RuntimeError(f"options HTTP {r.status_code}")
            s, crumb = sess2
            url = f"https://query2.finance.yahoo.com/v7/finance/options/{fetch_sym}?crumb={crumb}"
            if exp_ts:
                url += f"&date={exp_ts}"
            r = s.get(url, timeout=20)
        r.raise_for_status()
        result = ((r.json().get("optionChain") or {}).get("result") or [None])[0]
        if not result:
            return None
        payload = {
            "symbol": symbol.upper(),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "quote": result.get("quote") or {},
            "expirationDates": result.get("expirationDates") or [],
            "options": result.get("options") or [],
        }
        try:
            path.write_text(json.dumps(payload))
        except Exception:  # noqa: BLE001
            pass
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.debug("yahoo option chain %s: %s", fetch_sym, exc)
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None


def list_expiries(chain: dict[str, Any]) -> list[tuple[str, int]]:
    """Return (YYYY-MM-DD, dte) sorted by dte."""
    today = date.today()
    out: list[tuple[str, int]] = []
    for ts in chain.get("expirationDates") or []:
        try:
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except Exception:  # noqa: BLE001
            continue
        out.append((d.isoformat(), (d - today).days))
    out.sort(key=lambda x: x[1])
    return out


def pick_challenge_contract(
    symbol: str,
    spot: float,
    *,
    right: str = "C",
    yahoo_symbol: str | None = None,
    min_dte: int = 60,
    max_dte: int = 450,
    otm_pct_max: float = 8.0,
    itm_pct_max: float = 2.0,
    prefer_dte: int = 180,
) -> dict[str, Any] | None:
    """Select a liquid-ish swing/LEAP call or put with live/last option price."""
    right = right.upper()
    root = fetch_option_chain(symbol, yahoo_symbol=yahoo_symbol)
    if not root:
        return None

    q = root.get("quote") or {}
    live_spot = q.get("regularMarketPrice") or q.get("postMarketPrice") or spot
    try:
        live_spot = float(live_spot)
    except Exception:  # noqa: BLE001
        live_spot = spot
    if live_spot <= 0:
        return None

    expiries = list_expiries(root)
    targets = [(e, d) for e, d in expiries if min_dte <= d <= max_dte]
    if not targets:
        targets = [(e, d) for e, d in expiries if 45 <= d <= 550]
    if not targets:
        return None
    targets.sort(key=lambda x: abs(x[1] - prefer_dte))

    best: dict[str, Any] | None = None
    best_rank = -1e18

    for expiry, dte in targets[:5]:
        chain = fetch_option_chain(symbol, expiry=expiry, yahoo_symbol=yahoo_symbol)
        if not chain:
            continue
        opts = (chain.get("options") or [{}])[0]
        table = opts.get("calls") if right == "C" else opts.get("puts")
        if not table:
            continue
        for row in table:
            strike = float(row.get("strike") or 0)
            if strike <= 0:
                continue
            mny = (strike - live_spot) / live_spot * 100.0
            if right == "C":
                if mny < -itm_pct_max or mny > otm_pct_max:
                    continue
                otm_target = 3.0
            else:
                if mny > itm_pct_max or mny < -otm_pct_max:
                    continue
                otm_target = -3.0

            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            mark_source = "ask"
            if ask <= 0 and last > 0:
                ask = last
                bid = bid or round(last * 0.95, 2)
                mark_source = "last"
            if ask <= 0:
                continue
            oi = int(row.get("openInterest") or 0)
            vol = int(row.get("volume") or 0)
            if oi < 10 and vol < 5 and last <= 0:
                continue
            spread = ((ask - bid) / ask) if ask and bid > 0 else 0.0
            rank = (
                50.0
                - abs(mny - otm_target) * 4.0
                - abs(dte - prefer_dte) * 0.04
                - spread * 25.0
                + min(25.0, oi / 200.0)
                + min(12.0, vol / 50.0)
                + (5.0 if mark_source == "ask" else 0.0)
            )
            if rank > best_rank:
                best_rank = rank
                best = {
                    "symbol": symbol.upper(),
                    "right": right,
                    "contract": str(row.get("contractSymbol") or ""),
                    "expiry": expiry,
                    "dte": dte,
                    "strike": strike,
                    "spot": round(live_spot, 4),
                    "bid": round(bid, 2) if bid > 0 else None,
                    "ask": round(ask, 2),
                    "last": round(last, 2) if last > 0 else None,
                    "mark_source": mark_source,
                    "moneyness_pct": round(mny, 3),
                    "open_interest": oi,
                    "volume": vol,
                    "style": "leap" if dte >= 180 else "swing",
                    "live": True,
                    "suggested_zone": False,
                }
        # small pause between expiries to reduce 429s
        time.sleep(0.15)
    return best
