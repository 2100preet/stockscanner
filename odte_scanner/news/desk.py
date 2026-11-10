"""📰 News Desk · alert only.

Polls public news feeds on its own daemon thread, tags each headline, and
sends one short Telegram alert per ticker per event. It also keeps a shadow
log of one rule (earnings miss/cut gap-down → 9:45 ET put), marked with live
option quotes.

Alert only: this module never places, sizes, routes, cancels, or changes an
order, and never imports the Webull broker or the auto-trader. SPX is alert
only like everything else here. The shadow log is bookkeeping, not trading.

Memory: no in-memory caches beyond small bounded dicts. Feeds are read with a
byte cap, the seen-id list is capped, today's events are capped, and the
shadow history is read line by line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from odte_scanner.news import feeds as F
from odte_scanner.news.classify import classify, display_headline, earnings_by_move, headline_of, tag

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DAY_STORE = ROOT / "outputs" / "news_desk_day.json"
SHADOW_STORE = ROOT / "outputs" / "news_shadow.jsonl"

LABEL = "📰 News Desk · alert only"
NOTE = (
    "Market-moving news from SEC 8-K filings, PR Newswire, FDA, and the Nasdaq earnings "
    "calendar. Alert only: this desk never places, sizes, or exits an order. "
    "Shadow stats are a paper record of one rule, not trades."
)

# --- polling
POLL_SEC_DEFAULT = 30
POLL_SEC_MIN = 30
POLL_SEC_MAX = 60
QUIET_POLL_SEC = 300          # nights and weekends
SOURCE_EVERY_SEC = {"edgar8k": 0, "edgar6k": 120, "prnewswire": 60, "fda": 300, "nasdaq": 600, "polygon": 300}
TEXT_FETCHES_PER_CYCLE = 12   # SEC exhibit reads per pass
# --- alerting
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 1_000_000
STALE = timedelta(minutes=30)
# Nasdaq's calendar rarely publishes the EPS actual the same day, so an earnings
# release waits this long for it, then is tagged by its extended-hours move.
EARN_PENDING_MAX = timedelta(minutes=10)
SEND_START_CT = dtime(5, 0)
SEND_END_CT = dtime(20, 0)
SEND_MIN_GAP_SEC = 4.0
SEND_MAX_PER_HOUR = 10
SEND_MAX_PER_DAY = 40
# --- memory bounds
MAX_EVENTS = 300
MAX_SEEN = 3000
MAX_PENDING = 50
MAX_SHADOW_OPEN = 30
QUOTE_CACHE_MAX = 400
QUOTE_TTL_SEC = 600
# --- shadow rule (earnings miss/cut gap-down → put at 9:45 ET)
SHADOW_TYPES = {"EARN_MISS", "EARN_CUT", "EARN_GAP_DOWN"}
SHADOW_GAP_MIN = -0.12
SHADOW_GAP_MAX = -0.02
SHADOW_ENTRY_CT = dtime(8, 45)
SHADOW_ENTRY_LATE_CT = dtime(9, 0)
SHADOW_FLAT_CT = dtime(10, 30)
SHADOW_DELTA_LO = 0.40
SHADOW_DELTA_HI = 0.50
SHADOW_MIN_DTE = 5
SHADOW_MARK_GAP_SEC = 30
SHADOW_RULE = {
    "types": sorted(SHADOW_TYPES),
    "gap_pct": [round(SHADOW_GAP_MIN * 100, 1), round(SHADOW_GAP_MAX * 100, 1)],
    "entry": "9:45 ET (8:45 CT) if still below the open, at the put's Ask (Limit)",
    "stop": "underlying reclaims the 9:30 ET open",
    "flat": "10:30 CT",
    "delta": [SHADOW_DELTA_LO, SHADOW_DELTA_HI],
    "min_dte": SHADOW_MIN_DTE,
    "orders": "none — shadow log only",
}

# Dedupe families: one alert per ticker per family per day.
_FAMILY = {
    "EARN_BEAT": "EARN", "EARN_MISS": "EARN", "EARN_RAISE": "EARN", "EARN_CUT": "EARN",
    "EARN_GAP_DOWN": "EARN", "EARN_GAP_UP": "EARN", "EARN_OUT": "EARN",
    "GUIDE_RAISE": "GUIDE", "GUIDE_CUT": "GUIDE", "MA_TARGET": "MA", "MA_ACQUIRER": "MA",
}
# Event types alerted even outside the app's scan universe (still need price/volume).
CATALYST_TYPES = {"FDA_APPROVAL", "FDA_CRL", "TRIAL_FAIL", "TRIAL_DATA", "MA_TARGET", "MA_ACQUIRER"}
# 8-K items worth reading the exhibit for.
_EDGAR_ITEMS = {"1.01", "1.03", "1.05", "2.01", "2.02", "2.05", "2.06", "3.02", "4.02", "5.02", "7.01", "8.01"}

_LOCK = threading.RLock()


# ================================================================= helpers


def _ct(moment: datetime | None = None) -> datetime:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CT)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ct(value)
    if not value:
        return None
    try:
        return _ct(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _hm(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'} CT"


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def telegram_enabled() -> bool:
    return env_flag("NEWS_DESK_TELEGRAM", True)


def poll_seconds() -> int:
    try:
        value = int(float(os.environ.get("NEWS_DESK_POLL_SEC") or POLL_SEC_DEFAULT))
    except ValueError:
        value = POLL_SEC_DEFAULT
    return max(POLL_SEC_MIN, min(POLL_SEC_MAX, value))


def is_weekday(moment: datetime) -> bool:
    return _ct(moment).weekday() < 5


def quiet_hours(moment: datetime) -> bool:
    """Nights and weekends: slower polling and no Telegram sends."""
    local = _ct(moment)
    return local.weekday() >= 5 or not (SEND_START_CT <= local.time() < SEND_END_CT)


def next_weekday(day: date) -> date:
    nxt = day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def session_of(moment: datetime) -> str:
    local = _ct(moment)
    if local.weekday() >= 5:
        return "closed"
    t = local.time()
    if t < dtime(8, 30):
        return "pre"
    if t < dtime(15, 0):
        return "rth"
    return "post"


def reaction_day(moment: datetime) -> date:
    """The session the news trades in: same day before the close, else the next weekday."""
    local = _ct(moment)
    if local.weekday() >= 5 or local.time() >= dtime(15, 0):
        return next_weekday(local.date())
    return local.date()


# ================================================================= state


def read_state(path: Path | None = None) -> dict[str, Any]:
    from odte_scanner.json_util import load_json_state

    blob = load_json_state(path or DAY_STORE)
    return blob if isinstance(blob, dict) else {}


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    from odte_scanner.json_util import atomic_write_text, dumps_strict

    dest = path or DAY_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, dumps_strict(state, default=str))


def _fresh_state(day: str, carry: dict[str, Any] | None = None) -> dict[str, Any]:
    carry = carry or {}
    shadow = [r for r in carry.get("shadow") or [] if isinstance(r, dict)]
    return {
        "day": day,
        "events": [],
        "seen": list(carry.get("seen") or [])[-MAX_SEEN // 2:],
        "alerted": [],
        "pending": [p for p in carry.get("pending") or [] if isinstance(p, dict)][-MAX_PENDING:],
        "sends": [],
        "sent_today": 0,
        "sources": dict(carry.get("sources") or {}),
        "shadow": shadow[-MAX_SHADOW_OPEN:],
        "polygon_off_day": carry.get("polygon_off_day"),
    }


def _trim(state: dict[str, Any]) -> None:
    state["seen"] = list(state.get("seen") or [])[-MAX_SEEN:]
    state["events"] = list(state.get("events") or [])[-MAX_EVENTS:]
    state["pending"] = list(state.get("pending") or [])[-MAX_PENDING:]
    state["alerted"] = list(state.get("alerted") or [])[-MAX_EVENTS:]


def read_shadow_history(path: Path | None = None, limit: int = 2000) -> list[dict[str, Any]]:
    """Last ``limit`` closed shadow rows, read line by line."""
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with (path or SHADOW_STORE).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return list(rows)


def _append_shadow(row: dict[str, Any], path: Path | None = None) -> None:
    from odte_scanner.json_util import dumps_strict

    dest = path or SHADOW_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(dumps_strict(row, default=str) + "\n")


# ================================================================= quotes (bounded cache)


class QuoteBook:
    """Small TTL cache of Tradier equity quotes. Bounded to QUOTE_CACHE_MAX symbols."""

    def __init__(self, fetch: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or _tradier_quotes
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, symbols: Iterable[str], *, max_age: float = QUOTE_TTL_SEC) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        want = [s for s in {str(x).upper() for x in symbols if x} if s not in self._rows or now - self._rows[s][0] > max_age]
        if want:
            try:
                fresh = self._fetch(want) or {}
            except Exception as exc:  # noqa: BLE001
                logger.info("news desk quotes failed: %s", F.redact_error(str(exc)))
                fresh = {}
            for sym, row in fresh.items():
                if isinstance(row, dict):
                    self._rows[str(sym).upper()] = (now, row)
        if len(self._rows) > QUOTE_CACHE_MAX:
            for sym, _ in sorted(self._rows.items(), key=lambda kv: kv[1][0])[: len(self._rows) - QUOTE_CACHE_MAX]:
                self._rows.pop(sym, None)
        return {s: self._rows[s][1] for s in {str(x).upper() for x in symbols if x} if s in self._rows}

    def __len__(self) -> int:
        return len(self._rows)


def _tradier_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    from odte_scanner.data.tradier import access_token_from_env, fetch_quotes

    if not access_token_from_env():
        return {}
    res = fetch_quotes(symbols)
    return res.get("quotes") or {} if isinstance(res, dict) else {}


def liquidity_check(quote: dict[str, Any] | None) -> tuple[bool | None, str]:
    """(passes, why). None when there is no quote to judge."""
    if not quote:
        return None, "no quote"
    price = _num(quote.get("last")) or _num(quote.get("prevclose")) or _num(quote.get("close"))
    avg = _num(quote.get("average_volume"))
    if not price:
        return None, "no price"
    if price < MIN_PRICE:
        return False, f"price ${price:.2f} < ${MIN_PRICE:.0f}"
    if avg is None or avg <= 0:
        return None, "no average volume"
    if avg < MIN_AVG_VOLUME:
        return False, f"avg volume {avg / 1e6:.2f}M < 1M"
    return True, "liquid"


def move_from_quote(quote: dict[str, Any] | None, session: str, extended_last: float | None = None) -> tuple[float | None, str | None]:
    """(pct, label) vs the prior close. Premarket/after-hours use the extended-hours print when known."""
    if not quote:
        return None, None
    prev = _num(quote.get("prevclose"))
    if session == "rth":
        last = _num(quote.get("last"))
        base = prev
        label = "Today"
    elif session == "post":
        last = extended_last
        base = _num(quote.get("last")) or _num(quote.get("close"))
        label = "After hours"
    else:
        last = extended_last
        # Before the open Tradier's last is the prior session's final print.
        base = _num(quote.get("last")) or prev or _num(quote.get("close"))
        label = "Premarket" if session == "pre" else "Last"
    if not last or not base:
        return None, None
    pct = (last / base - 1.0) * 100.0
    return round(pct, 2), f"{label} {pct:+.1f}%"


def _extended_last(symbol: str, moment: datetime) -> float | None:
    """Latest pre/after-hours 1-minute close from Tradier. One small request per alert."""
    try:
        from odte_scanner.data.tradier import access_token_from_env, fetch_timesales

        if not access_token_from_env():
            return None
        et = moment.astimezone(ZoneInfo("America/New_York"))
        start = (et - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        end = et.strftime("%Y-%m-%d %H:%M")
        res = fetch_timesales(symbol, interval="1min", start=start, end=end, session_filter="all", timeout=8.0)
        bars = res.get("bars") or res.get("data") or []
        if bars:
            last = bars[-1]
            return _num(last.get("close") if isinstance(last, dict) else None)
    except Exception as exc:  # noqa: BLE001
        logger.info("news desk extended print %s: %s", symbol, F.redact_error(str(exc)))
    return None


# ================================================================= runtime (process-wide, small)


class Runtime:
    """Process-wide handles. Everything here is bounded."""

    def __init__(self) -> None:
        self.cik = F.CikMap()
        self.quotes = QuoteBook()
        self.universe: frozenset[str] = frozenset()
        self.earnings: dict[str, dict[str, dict[str, Any]]] = {}  # day -> ticker -> row (today + yesterday)
        self.last_poll: dict[str, float] = {}
        self.last_send_mono = 0.0


RUNTIME = Runtime()


def load_universe(config_path: str | None = None) -> frozenset[str]:
    try:
        from odte_scanner.config import load_config
        from odte_scanner.data.universe import resolve_scan_universe

        cfg = load_config(config_path)
        return frozenset(s.upper() for s in resolve_scan_universe(cfg, mode="all"))
    except Exception as exc:  # noqa: BLE001
        logger.info("news desk universe failed: %s", exc)
        return frozenset()


# ================================================================= feed gathering


def _due(rt: Runtime, name: str, moment_mono: float, quiet: bool, pending: bool = False) -> bool:
    every = SOURCE_EVERY_SEC.get(name, 60)
    if name == "nasdaq" and pending:
        every = min(every, 180)  # an earnings release is waiting on its EPS surprise
    if quiet:
        every = max(every, QUIET_POLL_SEC)
    last = rt.last_poll.get(name)
    return last is None or moment_mono - last >= every - 1


def _source_status(state: dict[str, Any], name: str, *, ok: bool, n: int = 0, error: str | None = None,
                   moment: datetime) -> None:
    src = dict((state.setdefault("sources", {})).get(name) or {})
    src["ok"] = ok
    src["checked_at"] = moment.isoformat()
    if ok:
        src["last_ok"] = moment.isoformat()
        src["n"] = n
        src.pop("error", None)
        src["fails"] = 0
    else:
        src["error"] = F.redact_error(error or "failed")
        src["fails"] = int(src.get("fails") or 0) + 1
    state["sources"][name] = src


def gather(
    state: dict[str, Any],
    moment: datetime,
    *,
    rt: Runtime,
    fetchers: dict[str, Callable[[], Any]],
    force: bool = False,
) -> list[dict[str, Any]]:
    """Poll every due source. A failing source is recorded and skipped."""
    items: list[dict[str, Any]] = []
    quiet = quiet_hours(moment)
    mono = time.monotonic()
    day = _ct(moment).date().isoformat()
    for name, fn in fetchers.items():
        if not force and not _due(rt, name, mono, quiet, bool(state.get("pending"))):
            continue
        if name == "polygon" and state.get("polygon_off_day") == day:
            continue
        rt.last_poll[name] = mono
        try:
            got = fn()
        except F.FeedAuthError as exc:
            state["polygon_off_day"] = day
            _source_status(state, name, ok=False, error=f"key rejected ({exc}); off for today", moment=moment)
            continue
        except Exception as exc:  # noqa: BLE001
            _source_status(state, name, ok=False, error=str(exc), moment=moment)
            logger.info("news desk source %s failed: %s", name, F.redact_error(str(exc)))
            continue
        if name == "nasdaq":
            if isinstance(got, dict):
                rt.earnings[day] = got
                for old in sorted(rt.earnings)[:-2]:
                    rt.earnings.pop(old, None)
                _source_status(state, name, ok=True, n=len(got), moment=moment)
            continue
        rows = [r for r in (got or []) if isinstance(r, dict)][:200]
        _source_status(state, name, ok=True, n=len(rows), moment=moment)
        items.extend(rows)
    return items


def default_fetchers(*, polygon: bool = True) -> dict[str, Callable[[], Any]]:
    out: dict[str, Callable[[], Any]] = {
        "edgar8k": lambda: F.fetch_edgar("8-K", 100),
        "edgar6k": lambda: F.fetch_edgar("6-K", 40),
        "prnewswire": F.fetch_prn,
        "fda": F.fetch_fda,
        "nasdaq": lambda: F.fetch_nasdaq_earnings(_ct().date().isoformat()),
    }
    if polygon and F.polygon_key():
        out["polygon"] = lambda: F.fetch_polygon_news(50)
    return out


def eps_surprise(rt: Runtime, ticker: str, moment: datetime) -> float | None:
    local = _ct(moment).date()
    for day in (local.isoformat(), (local - timedelta(days=1)).isoformat()):
        row = (rt.earnings.get(day) or {}).get(ticker)
        if row and row.get("surprise_pct") is not None:
            return float(row["surprise_pct"])
    return None


# ================================================================= event building


def _event_key(day: str, ticker: str | None, kind: str) -> str:
    return f"{day}|{ticker or '-'}|{_FAMILY.get(kind, kind)}"


def _resolve(item: dict[str, Any], rt: Runtime) -> list[str]:
    if item.get("source") == "edgar":
        tk = rt.cik.ticker(item.get("cik"))
        return [tk] if tk else []
    return [str(t).upper() for t in item.get("tickers") or [] if t][:4]


_FORM_COVER = re.compile(r"SECURITIES AND EXCHANGE COMMISSION|FORM (8-K|6-K)\b|CURRENT REPORT", re.I)
ITEM_NAMES = {
    "1.01": "material agreement",
    "1.03": "bankruptcy",
    "1.05": "cybersecurity incident",
    "2.01": "completed acquisition or sale",
    "2.02": "results",
    "2.05": "exit / restructuring costs",
    "2.06": "impairment",
    "3.02": "unregistered stock sale",
    "4.02": "non-reliance on past financials",
    "5.02": "officer / director change",
    "7.01": "Reg FD disclosure",
    "8.01": "other events",
}


def edgar_headline(item: dict[str, Any]) -> str:
    names = [ITEM_NAMES[i] for i in item.get("items") or [] if i in ITEM_NAMES]
    what = ", ".join(names) if names else "filing"
    return f"{item.get('company') or 'Filer'} files {item.get('form') or '8-K'}: {what}"


def build_events(
    state: dict[str, Any],
    items: list[dict[str, Any]],
    moment: datetime,
    *,
    rt: Runtime,
    text_fn: Callable[[int, str], str] | None = None,
) -> list[dict[str, Any]]:
    """New items -> classified event dicts (not yet gated). Marks items seen."""
    seen = set(state.get("seen") or [])
    seen_order: list[str] = list(state.get("seen") or [])
    text_fn = text_fn or F.fetch_edgar_text
    fetched = 0
    out: list[dict[str, Any]] = []
    for item in items:
        iid = str(item.get("id") or "")
        if not iid or iid in seen:
            continue
        published = _when(item.get("published")) or _ct(moment)
        stale = _ct(moment) - published > STALE
        tickers = _resolve(item, rt)
        text = str(item.get("text") or "")
        headline = str(item.get("title") or "")
        if stale:
            # Old news (restart, slow feed, backfill): remember it, never alert or list it.
            seen.add(iid)
            seen_order.append(iid)
            continue
        if item.get("source") == "edgar":
            if not tickers:
                seen.add(iid)
                seen_order.append(iid)
                continue
            if not (set(item.get("items") or []) & _EDGAR_ITEMS) and item.get("form") != "6-K":
                seen.add(iid)
                seen_order.append(iid)
                continue
            if fetched >= TEXT_FETCHES_PER_CYCLE:
                continue  # leave unseen; read next pass
            fetched += 1
            adsh = iid.split(":", 1)[-1]
            try:
                text = text_fn(int(item["cik"]), adsh)
            except Exception as exc:  # noqa: BLE001
                logger.info("news desk exhibit %s failed: %s", adsh, F.redact_error(str(exc)))
                text = ""
            headline = headline_of(text, company=item.get("company")) or ""
            if not headline or _FORM_COVER.search(headline[:160]):
                headline = edgar_headline(item)  # no press release: describe the filing
        seen.add(iid)
        seen_order.append(iid)
        if not tickers:
            tickers = [None]  # e.g. an FDA release names a drug, not a ticker
        for tk in tickers:
            company = rt.cik.name(tk) if tk else None
            company = company or item.get("company")
            tag = classify(
                headline=headline,
                text=text,
                items=item.get("items"),
                form=item.get("form") or "",
                company=company,
                eps_surprise_pct=eps_surprise(rt, tk, moment) if tk else None,
                subjects=item.get("subjects"),
            )
            out.append(
                {
                    "ticker": tk,
                    "company": (company or "")[:80] or None,
                    "type": tag["type"],
                    "label": tag["label"],
                    "direction": tag["direction"],
                    "note": tag["note"],
                    "alertable": bool(tag["alert"]),
                    "headline": headline[:220],
                    "url": item.get("url"),
                    "source": item.get("source"),
                    "form": item.get("form"),
                    "items": list(item.get("items") or [])[:8],
                    "published_at": published.isoformat(),
                    "published_ct": _hm(published),
                    "seen_at": _ct(moment).isoformat(),
                    "stale": stale,
                    "session": session_of(published),
                    "reaction_day": reaction_day(published).isoformat(),
                    "eps_surprise_pct": eps_surprise(rt, tk, moment) if tk else None,
                    "text_guidance": text[:2500] if tag["type"] == "EARN_PENDING" else None,
                }
            )
    state["seen"] = seen_order[-MAX_SEEN:]
    return out


def _tag_by_move(ev: dict[str, Any], moment: datetime, rt: Runtime, quotes: dict[str, dict[str, Any]],
                 extended_fn: Callable[[str, datetime], float | None] | None) -> None:
    """No EPS surprise yet: tag the release by how the stock is trading vs the prior close."""
    tk = ev.get("ticker")
    session = session_of(moment)
    ext = (extended_fn or _extended_last)(tk, moment) if tk and session in {"pre", "post"} else None
    pct, label = move_from_quote(quotes.get(tk), session, ext)
    kind = earnings_by_move(pct)
    t = tag(kind)
    ev.update(type=kind, label=t["label"], direction=t["direction"], note=t["note"], alertable=t["alert"],
              move_pct=pct, move_label=label, tagged_by="extended-hours move (no EPS surprise published yet)")


def _recheck_pending(state: dict[str, Any], moment: datetime, rt: Runtime) -> list[dict[str, Any]]:
    """Earnings releases wait up to EARN_PENDING_MAX for the Nasdaq EPS surprise."""
    ready: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    for ev in state.get("pending") or []:
        tk = ev.get("ticker")
        sur = eps_surprise(rt, tk, moment) if tk else None
        first = _when(ev.get("seen_at")) or _ct(moment)
        if sur is not None:
            tag = classify(headline=ev.get("headline") or "", text=ev.get("text_guidance") or "",
                           items=ev.get("items") or ["2.02"], company=ev.get("company"), eps_surprise_pct=sur)
            ev = dict(ev, type=tag["type"], label=tag["label"], direction=tag["direction"], note=tag["note"],
                      alertable=bool(tag["alert"]), eps_surprise_pct=sur)
            ready.append(ev)
        elif _ct(moment) - first >= EARN_PENDING_MAX:
            ready.append(dict(ev, needs_move=True))
        else:
            keep.append(ev)
    state["pending"] = keep[-MAX_PENDING:]
    return ready


# ================================================================= alert text + sending


def format_alert(ev: dict[str, Any]) -> str:
    """Short Telegram alert. Always ends in '· alert only'."""
    head = f"📰 NEWS · {ev.get('ticker') or '—'} · {ev.get('label')}"
    if ev.get("eps_surprise_pct") is not None and str(ev.get("type", "")).startswith("EARN"):
        head += f" (EPS {float(ev['eps_surprise_pct']):+.1f}% vs est.)"
    lines = [head, display_headline(str(ev.get("headline") or ""))]
    meta = [m for m in [ev.get("published_ct"), ev.get("move_label")] if m]
    if meta:
        lines.append(" · ".join(meta))
    if ev.get("note"):
        lines.append(f"Playbook: {ev['note']}")
    link = str(ev.get("url") or "").strip()
    lines.append(f"{link} · alert only" if link else "· alert only")
    text = "\n".join(lines)
    return text.replace("Sure Winner", "Gold Setup").replace("sure winner", "Gold Setup")


def _default_send(text: str) -> dict[str, Any]:
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text, timeout=10.0)


def _rate_ok(state: dict[str, Any], moment: datetime, rt: Runtime) -> tuple[bool, str]:
    now_ts = _ct(moment).timestamp()
    sends = [float(t) for t in state.get("sends") or [] if now_ts - float(t) < 3600]
    state["sends"] = sends
    if int(state.get("sent_today") or 0) >= SEND_MAX_PER_DAY:
        return False, f"daily cap {SEND_MAX_PER_DAY} reached"
    if len(sends) >= SEND_MAX_PER_HOUR:
        return False, f"hourly cap {SEND_MAX_PER_HOUR} reached"
    return True, ""


def _gate(ev: dict[str, Any], moment: datetime, rt: Runtime, quotes: dict[str, dict[str, Any]],
          state: dict[str, Any]) -> tuple[bool, str]:
    """(send, why_not). Only decides alerting; recording happens either way."""
    tk = ev.get("ticker")
    if not ev.get("alertable"):
        return False, "not an alert type"
    if not tk:
        return False, "no ticker matched"
    if ev.get("stale"):
        return False, "older than 30 min when first seen"
    in_universe = tk in rt.universe
    if not in_universe and ev.get("type") not in CATALYST_TYPES:
        return False, "outside the scan universe (not a catalyst type)"
    ok, why = liquidity_check(quotes.get(tk))
    if ok is False:
        return False, why
    if ok is None and not in_universe:
        return False, f"liquidity unknown ({why})"
    if _event_key(_ct(moment).date().isoformat(), tk, ev["type"]) in set(state.get("alerted") or []):
        return False, "duplicate (already alerted today)"
    if quiet_hours(moment):
        return False, "quiet hours (nights/weekends): recorded, not sent"
    return True, ""


def _send(ev: dict[str, Any], state: dict[str, Any], moment: datetime, rt: Runtime,
          send: Callable[[str], dict[str, Any]] | None, telegram: bool | None) -> None:
    want = telegram if telegram is not None else telegram_enabled()
    if not want:
        ev["sent"] = False
        ev["why_not_sent"] = "NEWS_DESK_TELEGRAM off"
        return
    ok, why = _rate_ok(state, moment, rt)
    if not ok:
        ev["sent"] = False
        ev["why_not_sent"] = f"rate limit: {why}"
        return
    gap = SEND_MIN_GAP_SEC - (time.monotonic() - rt.last_send_mono)
    if gap > 0:
        time.sleep(min(gap, SEND_MIN_GAP_SEC))
    text = format_alert(ev)
    try:
        res = (send or _default_send)(text) or {}
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)[:200]}
    rt.last_send_mono = time.monotonic()
    ev["sent"] = bool(res.get("ok"))
    if ev["sent"]:
        state.setdefault("sends", []).append(_ct(moment).timestamp())
        state["sent_today"] = int(state.get("sent_today") or 0) + 1
    else:
        ev["why_not_sent"] = str(res.get("error") or ("telegram not configured" if res.get("skipped") else "send failed"))[:160]


# ================================================================= shadow rule


def _default_chain_pick(symbol: str, today: date) -> dict[str, Any] | None:
    """Put with |delta| 0.40–0.50 at the first expiry ≥5 DTE. Read only."""
    from odte_scanner.data.tradier import access_token_from_env, fetch_expirations, fetch_option_chain

    if not access_token_from_env():
        return None
    exps = fetch_expirations(symbol).get("dates") or []
    expiry = None
    for raw in sorted(exps):
        try:
            if (date.fromisoformat(raw[:10]) - today).days >= SHADOW_MIN_DTE:
                expiry = raw[:10]
                break
        except ValueError:
            continue
    if not expiry:
        return None
    chain = fetch_option_chain(symbol, expiry, greeks=True).get("options") or []
    best = None
    for opt in chain:
        if str(opt.get("option_type") or "").lower() != "put":
            continue
        greeks = opt.get("greeks") or {}
        delta = abs(_num(greeks.get("delta")) or 0.0)
        bid = _num(opt.get("bid")) or 0.0
        ask = _num(opt.get("ask")) or 0.0
        if not (SHADOW_DELTA_LO <= delta <= SHADOW_DELTA_HI) or bid <= 0 or ask <= 0:
            continue
        score = abs(delta - 0.45)
        if best is None or score < best[0]:
            best = (score, opt, delta, bid, ask)
    if best is None:
        return None
    _, opt, delta, bid, ask = best
    return {
        "contract": str(opt.get("symbol") or "").upper(),
        "strike": _num(opt.get("strike")),
        "expiry": expiry,
        "dte": (date.fromisoformat(expiry) - today).days,
        "delta": round(-delta, 3),
        "bid": bid,
        "ask": ask,
    }


def _default_option_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    from odte_scanner.options.contract_quote import fetch_contract_quote

    return fetch_contract_quote(row)


def _shadow_candidate(ev: dict[str, Any]) -> dict[str, Any] | None:
    if ev.get("type") not in SHADOW_TYPES or not ev.get("ticker") or ev.get("session") == "rth":
        return None
    if str(ev.get("ticker")).upper() in {"SPX", "XSP"}:
        return None  # index products stay alert-only and outside this rule
    return {
        "key": f"{ev['reaction_day']}|{ev['ticker']}",
        "ticker": ev["ticker"],
        "type": ev["type"],
        "headline": ev.get("headline"),
        "published_ct": ev.get("published_ct"),
        "reaction_day": ev["reaction_day"],
        "status": "watching",
    }


def _close_shadow(row: dict[str, Any], price: float, basis: str, reason: str, moment: datetime) -> None:
    entry = float(row["entry_ask"])
    pct = (price / entry - 1.0) * 100.0 if entry > 0 else 0.0
    row.update(
        status="closed",
        exit_price=round(price, 4),
        exit_label=f"{basis} ${price:.2f}",
        exit_ct=_hm(_ct(moment)),
        exit_at=_ct(moment).isoformat(),
        exit_reason=reason,
        pnl_pct=round(pct, 2),
        pnl_usd=round((price - entry) * 100.0, 2),
    )


def run_shadow(
    state: dict[str, Any],
    moment: datetime,
    *,
    rt: Runtime,
    quotes_fn: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    pick: Callable[[str, date], dict[str, Any] | None] | None = None,
    option_quote: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    shadow_path: Path | None = None,
) -> int:
    """Advance the shadow rule. Returns rows finalized this pass. Never orders."""
    local = _ct(moment)
    today = local.date().isoformat()
    rows = [r for r in state.get("shadow") or [] if isinstance(r, dict)]
    pick = pick or _default_chain_pick
    option_quote = option_quote or _default_option_quote
    finalized = 0
    active = [r for r in rows if r.get("status") in {"watching", "open"} and r.get("reaction_day") <= today]
    quotes: dict[str, dict[str, Any]] = {}
    if active and is_weekday(moment) and local.time() >= dtime(8, 30):
        symbols = sorted({r["ticker"] for r in active})
        if quotes_fn is not None:
            try:
                quotes = quotes_fn(symbols) or {}
            except Exception as exc:  # noqa: BLE001
                logger.info("news shadow quotes failed: %s", exc)
        else:
            quotes = rt.quotes.get(symbols, max_age=20)
    for row in rows:
        status = row.get("status")
        if status not in {"watching", "open"}:
            continue
        rday = str(row.get("reaction_day") or "")
        if rday > today:
            continue
        if rday < today:
            if status == "watching":
                row.update(status="no_trade", reason="session passed before the 9:45 ET check")
            else:
                price = _num(row.get("last_mark")) or float(row["entry_ask"])
                _close_shadow(row, price, "Last mark", "flat (10:30 CT missed; last mark)", moment)
            continue
        if not is_weekday(moment):
            row.update(status="no_trade", reason="market closed")
            continue
        q = quotes.get(row["ticker"]) or {}
        if status == "watching":
            if local.time() < SHADOW_ENTRY_CT:
                continue
            if local.time() >= SHADOW_ENTRY_LATE_CT:
                row.update(status="no_trade", reason="missed the 8:45–9:00 CT entry window")
                continue
            open_px = _num(q.get("open"))
            prev = _num(q.get("prevclose"))
            last = _num(q.get("last"))
            if not open_px or not prev or not last:
                row.update(status="no_trade", reason="no open/prior close/last (market closed or no quote)")
                continue
            gap = open_px / prev - 1.0
            row["gap_pct"] = round(gap * 100.0, 2)
            row["open"] = open_px
            if not (SHADOW_GAP_MIN <= gap <= SHADOW_GAP_MAX):
                row.update(status="no_trade", reason=f"gap {gap * 100:+.1f}% outside −12%…−2%")
                continue
            if last >= open_px:
                row.update(status="no_trade", reason="back at/above the open at 9:45 ET")
                continue
            try:
                picked = pick(row["ticker"], local.date())
            except Exception as exc:  # noqa: BLE001
                picked = None
                row["pick_error"] = str(exc)[:120]
            if not picked or not _num(picked.get("ask")):
                row.update(status="no_trade", reason="no put with |delta| 0.40–0.50 and ≥5 DTE with a live Ask")
                continue
            ask = float(picked["ask"])
            row.update(
                status="open",
                contract=picked.get("contract"),
                strike=picked.get("strike"),
                expiry=picked.get("expiry"),
                dte=picked.get("dte"),
                delta=picked.get("delta"),
                entry_ask=round(ask, 4),
                entry_label=f"Limit Ask ${ask:.2f}",
                entry_ct=_hm(local),
                entry_at=local.isoformat(),
                entry_underlying=last,
            )
            continue
        # open: stop on a reclaim of the open, flat at 10:30 CT
        last = _num(q.get("last"))
        open_px = _num(row.get("open"))
        flat_due = local.time() >= SHADOW_FLAT_CT
        reclaim = bool(last and open_px and last > open_px)
        last_mark_at = _when(row.get("last_mark_at"))
        if not (flat_due or reclaim) and last_mark_at and (local - last_mark_at).total_seconds() < SHADOW_MARK_GAP_SEC:
            continue
        try:
            oq = option_quote({"symbol": row["ticker"], "contract": row.get("contract"), "expiry": row.get("expiry"),
                               "strike": row.get("strike"), "right": "P"}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.info("news shadow option quote failed: %s", exc)
            oq = {}
        bid = _num(oq.get("bid"))
        if bid and bid > 0:
            row.update(last_mark=bid, last_mark_label=f"Bid ${bid:.2f}", last_mark_at=local.isoformat())
        if reclaim or flat_due:
            reason = "stop: reclaimed the open" if reclaim else "flat 10:30 CT"
            if bid and bid > 0:
                _close_shadow(row, bid, "Bid", reason, moment)
            else:
                price = _num(row.get("last_mark")) or float(row["entry_ask"])
                _close_shadow(row, price, "Last mark", reason + " (no live Bid)", moment)
    for row in rows:
        if row.get("status") in {"closed", "no_trade"} and not row.get("logged"):
            _append_shadow({k: v for k, v in row.items() if k != "logged"}, shadow_path)
            row["logged"] = True
            finalized += 1
    # keep only rows still in play plus today's finished rows (bounded)
    state["shadow"] = [r for r in rows if not r.get("logged") or r.get("reaction_day") == today][-MAX_SHADOW_OPEN:]
    return finalized


def shadow_stats(history: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in history if r.get("status") == "closed"]
    usd = [float(r.get("pnl_usd") or 0.0) for r in closed]
    pct = [float(r.get("pnl_pct") or 0.0) for r in closed]
    n = len(closed)
    gains = sum(x for x in usd if x > 0)
    losses = -sum(x for x in usd if x < 0)
    wins = sum(1 for x in usd if x > 0)
    return {
        "candidates": len(history),
        "trades": n,
        "no_trade": sum(1 for r in history if r.get("status") == "no_trade"),
        "wins": wins,
        "win_rate_pct": round(wins / n * 100.0, 1) if n else None,
        "avg_pct": round(sum(pct) / n, 2) if n else None,
        "pnl_usd_per_contract": round(sum(usd), 2),
        "profit_factor": round(gains / losses, 2) if losses > 0 else None,
    }


# ================================================================= one cycle


def run_cycle(
    *,
    now: datetime | None = None,
    store_path: Path | None = None,
    shadow_path: Path | None = None,
    rt: Runtime | None = None,
    fetchers: dict[str, Callable[[], Any]] | None = None,
    text_fn: Callable[[int, str], str] | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    telegram: bool | None = None,
    extended_fn: Callable[[str, datetime], float | None] | None = None,
    shadow_kwargs: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Poll due sources, classify, alert, and advance the shadow rule. Never orders."""
    rt = rt or RUNTIME
    moment = _ct(now)
    day = moment.date().isoformat()
    with _LOCK:
        state = read_state(store_path)
        if not state:
            state = _fresh_state(day)
        elif str(state.get("day") or "") != day:
            state = _fresh_state(day, carry=state)
        try:
            rt.cik.ensure()
        except Exception as exc:  # noqa: BLE001
            logger.info("news desk CIK map unavailable: %s", F.redact_error(str(exc)))
        try:
            return _cycle_body(state, moment, day, rt=rt, fetchers=fetchers, text_fn=text_fn, send=send,
                               telegram=telegram, extended_fn=extended_fn, shadow_kwargs=shadow_kwargs,
                               shadow_path=shadow_path, force=force)
        finally:
            # Always persist seen ids and sends, so an error can never cause a repeat alert.
            state["updated_at"] = moment.isoformat()
            _trim(state)
            write_state(state, store_path)


def _cycle_body(state: dict[str, Any], moment: datetime, day: str, *, rt: Runtime,
                fetchers: dict[str, Callable[[], Any]] | None, text_fn: Callable[[int, str], str] | None,
                send: Callable[[str], dict[str, Any]] | None, telegram: bool | None,
                extended_fn: Callable[[str, datetime], float | None] | None,
                shadow_kwargs: dict[str, Any] | None, shadow_path: Path | None, force: bool) -> dict[str, Any]:
    items = gather(state, moment, rt=rt, fetchers=fetchers if fetchers is not None else default_fetchers(), force=force)
    events = build_events(state, items, moment, rt=rt, text_fn=text_fn)
    # Only scan-universe names wait for a surprise; other earnings are not alert material.
    pending_new = [e for e in events if e["type"] == "EARN_PENDING" and e.get("ticker") in rt.universe]
    state["pending"] = list(state.get("pending") or []) + pending_new
    events = [e for e in events if e not in pending_new] + _recheck_pending(state, moment, rt)
    need = sorted({e["ticker"] for e in events if e.get("ticker") and (e.get("alertable") or e.get("needs_move"))})
    quotes = rt.quotes.get(need) if need else {}
    recorded_keys = {_record_key(day, e) for e in state.get("events") or []}
    sent = recorded = 0
    for ev in events:
        try:
            outcome = _handle_event(ev, state, moment, day, rt=rt, quotes=quotes, send=send, telegram=telegram,
                                    extended_fn=extended_fn, recorded_keys=recorded_keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("news desk event %s failed: %s", ev.get("ticker"), exc)
            continue
        sent += outcome == "sent"
        recorded += outcome in {"sent", "recorded"}
    try:
        finalized = run_shadow(state, moment, rt=rt, shadow_path=shadow_path, **(shadow_kwargs or {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("news shadow pass failed: %s", exc)
        finalized = 0
    return {"ok": True, "items": len(items), "recorded": recorded, "sent": sent, "shadow_finalized": finalized}


def _record_key(day: str, ev: dict[str, Any]) -> str:
    return _event_key(day, ev.get("ticker"), str(ev.get("type"))) + "|" + str(ev.get("headline"))[:60]


def _handle_event(ev: dict[str, Any], state: dict[str, Any], moment: datetime, day: str, *, rt: Runtime,
                  quotes: dict[str, dict[str, Any]], send: Callable[[str], dict[str, Any]] | None,
                  telegram: bool | None, extended_fn: Callable[[str, datetime], float | None] | None,
                  recorded_keys: set[str]) -> str:
    """Gate, alert, and record one event. Returns 'sent', 'recorded', or 'skipped'."""
    ev.pop("text_guidance", None)
    if ev.pop("needs_move", False):
        _tag_by_move(ev, moment, rt, quotes, extended_fn)
    if not (ev.get("alertable") or ev.get("type") in {"FDA_OTHER", "EARN_OUT"}):
        return "skipped"  # routine, dividends, law-firm spam: not listed
    go, why = _gate(ev, moment, rt, quotes, state)
    tk = ev.get("ticker")
    if go:
        if not ev.get("move_label"):
            session = session_of(moment)
            ext = (extended_fn or _extended_last)(tk, moment) if session in {"pre", "post"} else None
            ev["move_pct"], ev["move_label"] = move_from_quote(quotes.get(tk), session, ext)
        # Mark before sending: a crash after a send can never repeat it.
        state.setdefault("alerted", []).append(_event_key(day, tk, ev["type"]))
        _send(ev, state, moment, rt, send, telegram)
    else:
        ev["sent"] = False
        ev["why_not_sent"] = why
        if why == "duplicate (already alerted today)":
            return "skipped"
    # Shadow only names that passed every filter (sent, or held back only by the clock/limits).
    if go or why.startswith("quiet hours"):
        cand = _shadow_candidate(ev)
        if cand and cand["key"] not in {r.get("key") for r in state.get("shadow") or []}:
            state.setdefault("shadow", []).append(cand)
    rkey = _record_key(day, ev)
    if rkey in recorded_keys:
        return "skipped"  # same story from a second source
    recorded_keys.add(rkey)
    state.setdefault("events", []).append(ev)
    return "sent" if ev.get("sent") else "recorded"


# ================================================================= snapshot block


def _event_view(ev: dict[str, Any]) -> dict[str, Any]:
    keys = ("ticker", "label", "type", "direction", "headline", "url", "source", "published_ct", "move_label",
            "eps_surprise_pct", "note", "sent", "why_not_sent", "session")
    view = {k: ev.get(k) for k in keys}
    view["headline"] = display_headline(str(ev.get("headline") or ""))
    return view


def build_block(*, store_path: Path | None = None, shadow_path: Path | None = None) -> dict[str, Any]:
    state = read_state(store_path)
    history = read_shadow_history(shadow_path)
    today = [r for r in state.get("shadow") or [] if isinstance(r, dict)]
    events = list(reversed(state.get("events") or []))[:40]
    return {
        "ok": True,
        "label": LABEL,
        "alert_only": True,
        "orders": False,
        "day": state.get("day"),
        "events": [_event_view(e) for e in events],
        "n_events": len(state.get("events") or []),
        "sent_today": int(state.get("sent_today") or 0),
        "sources": state.get("sources") or {},
        "shadow": {
            "rule": SHADOW_RULE,
            "today": [
                {k: r.get(k) for k in ("ticker", "type", "status", "reason", "gap_pct", "contract", "entry_label",
                                       "entry_ct", "last_mark_label", "exit_label", "exit_ct", "exit_reason",
                                       "pnl_pct", "pnl_usd", "reaction_day")}
                for r in today
            ],
            "stats": shadow_stats(history),
            "shadow_only": True,
        },
        "telegram": telegram_enabled(),
        "updated_at": state.get("updated_at"),
        "note": NOTE,
    }


def public_block() -> dict[str, Any]:
    try:
        return build_block()
    except Exception as exc:  # noqa: BLE001
        logger.warning("news desk block failed: %s", exc)
        return {"ok": False, "label": LABEL, "alert_only": True, "events": [], "error": str(exc)[:200],
                "note": "News Desk unavailable."}


# ================================================================= worker thread

_worker_started = False
_worker_lock = threading.Lock()


def _sleep_for(moment: datetime, state_path: Path | None = None) -> float:
    local = _ct(moment)
    if is_weekday(moment) and dtime(8, 40) <= local.time() <= dtime(10, 35):
        state = read_state(state_path)
        if any(r.get("status") in {"watching", "open"} for r in state.get("shadow") or []):
            return float(POLL_SEC_MIN)
    if quiet_hours(moment):
        return float(QUIET_POLL_SEC)
    return float(poll_seconds())


def _worker_loop(config_path: str | None) -> None:
    logger.info("News desk started (alert only) poll=%ss telegram=%s", poll_seconds(), telegram_enabled())
    RUNTIME.universe = load_universe(config_path)
    while True:
        started = time.monotonic()
        try:
            run_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("news desk cycle failed: %s", exc)
        try:
            pause = _sleep_for(datetime.now(timezone.utc))
        except Exception:  # noqa: BLE001
            pause = float(poll_seconds())
        time.sleep(max(5.0, pause - (time.monotonic() - started)))


def news_desk_enabled(config_path: str | None = None) -> bool:
    if not env_flag("NEWS_DESK_LOOP", True):
        return False
    try:
        from odte_scanner.config import load_config

        cfg = load_config(config_path)
        return bool((cfg.get("actions") or {}).get("news_desk_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def start_news_desk_worker(config_path: str | None = None) -> bool:
    """Start the news loop once per process on its own daemon thread."""
    global _worker_started
    # Same rule as live_desk: Pages / offline export never start workers.
    if (os.environ.get("SIGNAL_DESK_OFFLINE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if not news_desk_enabled(config_path):
        logger.info("News desk disabled (news_desk_enabled false or NEWS_DESK_LOOP=0)")
        return False
    with _worker_lock:
        if _worker_started:
            return False
        _worker_started = True
    threading.Thread(target=_worker_loop, args=(config_path,), name="news-desk-worker", daemon=True).start()
    return True
