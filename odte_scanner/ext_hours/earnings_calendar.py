"""Which of the 27 S2 names report AFTER the close on a given day.

Sources (union, so one outage doesn't hide a report):
1. Nasdaq earnings calendar ``api.nasdaq.com/api/calendar/earnings?date=D``:
   one request per day, rows tagged ``time-after-hours``.
2. Yahoo ``get_earnings_dates`` (the source the S2 backtest used): a release
   stamped 16:00 ET or later counts as after the close. Only asked when
   Nasdaq failed or tagged one of our names ``time-not-supplied``.

The result is cached per day in ``outputs/ah_earnings_calendar.json``. One
fetch a day; failures fail open (we keep the last good cache and record
the error, the app never crashes).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable

import requests

from odte_scanner.ext_hours.common import ET, JsonLedger, resolve_path

logger = logging.getLogger(__name__)

S2_UNIVERSE = (
    "NVDA TSLA AAPL META AMZN MSFT GOOGL AMD AVGO NFLX ORCL MU PLTR COIN CRM ADBE "
    "SMCI MSTR PANW SNOW LULU DELL MRVL UBER SHOP NKE INTC"
).split()

NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
_HEADERS = {"User-Agent": "Mozilla/5.0 (stockscanner shadow desk)", "Accept": "application/json"}


def fetch_nasdaq_day(day: date, *, timeout: float = 12.0) -> dict[str, str]:
    """{SYMBOL: time-tag} for every company Nasdaq lists on ``day``."""
    r = requests.get(NASDAQ_URL, params={"date": day.isoformat()}, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    rows = ((r.json() or {}).get("data") or {}).get("rows") or []
    return {str(x.get("symbol") or "").upper(): str(x.get("time") or "") for x in rows if x.get("symbol")}


def fetch_yahoo_amc(symbols: list[str], day: date) -> set[str]:
    """Names whose Yahoo earnings timestamp is ``day`` at/after 16:00 ET."""
    import yfinance as yf

    out: set[str] = set()
    for sym in symbols:
        try:
            df = yf.Ticker(sym).get_earnings_dates(limit=8)
        except Exception:  # noqa: BLE001
            continue
        if df is None or len(df) == 0:
            continue
        for ts in df.index:
            try:
                t = ts.tz_convert(ET) if ts.tzinfo else ts.tz_localize(ET)
            except Exception:  # noqa: BLE001
                continue
            if t.date() == day and t.hour >= 16:
                out.add(sym)
    return out


class EarningsCalendar:
    def __init__(
        self,
        *,
        path=None,
        nasdaq: Callable[[date], dict[str, str]] | None = None,
        yahoo: Callable[[list[str], date], set[str]] | None = None,
        universe: list[str] | None = None,
    ) -> None:
        self.ledger = JsonLedger(path or resolve_path("AH_EARNINGS_CALENDAR_PATH", "outputs/ah_earnings_calendar.json"), {"days": {}})
        self.nasdaq = nasdaq or fetch_nasdaq_day
        self.yahoo = yahoo or fetch_yahoo_amc
        self.universe = list(universe or S2_UNIVERSE)

    def amc_names(self, day: date, *, refresh: bool = False) -> dict[str, Any]:
        """Cached once per day. Returns {'names': [...], 'sources': {...}, 'errors': [...]}."""
        data = self.ledger.load()
        days = data.setdefault("days", {})
        key = day.isoformat()
        cached = days.get(key)
        if cached and not refresh and cached.get("complete"):
            return cached
        names: set[str] = set()
        sources: dict[str, str] = {}
        errors: list[str] = []
        need_yahoo: list[str] = []
        try:
            tags = self.nasdaq(day)
            for sym in self.universe:
                tag = tags.get(sym)
                if tag == "time-after-hours":
                    names.add(sym)
                    sources[sym] = "nasdaq"
                elif tag is not None and tag not in ("time-pre-market",):
                    need_yahoo.append(sym)  # listed, time not supplied
            nasdaq_ok = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"nasdaq: {type(exc).__name__}")
            need_yahoo = list(self.universe)
            nasdaq_ok = False
        yahoo_ok = True
        if need_yahoo:
            try:
                for sym in self.yahoo(need_yahoo, day):
                    names.add(sym)
                    sources.setdefault(sym, "yahoo")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"yahoo: {type(exc).__name__}")
                yahoo_ok = False
        complete = nasdaq_ok or yahoo_ok
        if not complete and cached:
            cached = dict(cached)
            cached["errors"] = errors
            return cached  # fail open: keep the last good answer
        entry = {
            "date": key,
            "names": sorted(names),
            "sources": sources,
            "errors": errors,
            "complete": complete,
            "fetched_at": datetime.now(ET).isoformat(timespec="seconds"),
        }
        days[key] = entry
        for old in sorted(days)[:-30]:
            days.pop(old, None)
        self.ledger.save(data)
        return entry
