"""🧬 Biotech Catalyst Watch · alert only.

Small- and mid-cap biotech catalysts the News Desk drops because of its
$10 / 1M average-volume gate: trial readouts, FDA decisions (approval / CRL),
PDUFA dates, AdComs, plus volume spikes on a small watchlist (SLS, ...).

It rides the News Desk cycle: it reads the events the desk already built
(no extra SEC feed polling) and the desk's bounded Tradier quote cache. The
only extra SEC call is a small, cached submissions lookup for the dilution
check, capped per cycle.

Alert only: never places, sizes, routes, cancels, or changes an order, and
never imports the Webull broker or the auto-trader.

Backtest (Nov 17, 2026, 2015–2026 EDGAR 8-Ks + daily prices, split halves):
buying after a positive trial / FDA 8-K, after a +10% confirmation day, or
after a 3x-volume +10% spike lost money in both halves even before slippage.
Hence alert only and the "don't chase" playbook line.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DAY_STORE = ROOT / "outputs" / "biotech_watch_day.json"

LABEL = "🧬 Biotech Catalyst Watch · alert only"
MIN_PRICE = 1.0
SPIKE_VOL_X = 3.0
SPIKE_MOVE_PCT = 10.0
SPIKE_EVERY_SEC = 300
DILUTION_LOOKBACK_DAYS = 120
DILUTION_FORMS = ("S-3", "S-3ASR", "S-1", "F-3", "424B5", "424B3", "424B4", "424B7")
DILUTION_LOOKUPS_PER_CYCLE = 3
DILUTION_CACHE_MAX = 200
DILUTION_TTL_SEC = 12 * 3600
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_MAX_BYTES = 4_000_000
SEND_MAX_PER_HOUR = 6
SEND_MAX_PER_DAY = 20
MAX_EVENTS = 80
MAX_WATCH = 60
RECENT_CATALYST_DAYS = 10
SEND_START_CT = dtime(5, 0)
SEND_END_CT = dtime(20, 0)

DEFAULT_WATCHLIST = ("SLS",)
MAX_UPCOMING = 60
UPCOMING_UNDATED_DAYS = 180

KIND_LABELS = {
    "FDA_APPROVAL": "FDA approval",
    "FDA_CRL": "FDA rejection (CRL)",
    "FDA_OTHER": "FDA update",
    "TRIAL_DATA": "Trial readout (positive wording)",
    "TRIAL_FAIL": "Trial failure",
    "TOPLINE": "Topline data",
    "PDUFA": "PDUFA date",
    "ADCOM": "FDA advisory committee",
    "VOLUME_SPIKE": "Volume spike",
}
NOTE = ("Backtest 2015–2026: buying after positive biotech catalysts or 3x-volume spikes lost money in both "
        "halves even before costs — watch, don't chase.")

_BIO_TYPES = {"FDA_APPROVAL", "FDA_CRL", "FDA_OTHER", "TRIAL_DATA", "TRIAL_FAIL"}
_PDUFA = re.compile(r"\bPDUFA\b|target action date|prescription drug user fee", re.I)
_ADCOM = re.compile(r"advisory committee|\bAdCom\b|\bODAC\b", re.I)
_TOPLINE = re.compile(r"top[- ]?line (results|data)|primary endpoint|interim (analysis|results|data)|"
                      r"phase (1|2|3|i|ii|iii|1b|2b|2a)\b.{0,60}(results|data|readout)", re.I)
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = re.compile(r"(" + _MONTH + r"\s+\d{1,2},?\s+20\d\d)")
_QUARTER = re.compile(r"\b((?:first|second|third|fourth|1Q|2Q|3Q|4Q|Q[1-4])\s*(?:quarter\s*(?:of\s*)?)?(?:20\d\d)|"
                      r"(?:mid|early|late|end of|first half|second half|1H|2H)[- ](?:of )?20\d\d)\b", re.I)


# ================================================================= helpers


def _ct(moment: datetime | None = None) -> datetime:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def read_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DAY_STORE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or DAY_STORE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.info("biotech watch state write failed: %s", exc)


def _fresh(day: str, carry: dict[str, Any] | None = None) -> dict[str, Any]:
    carry = carry or {}
    recent = {k: v for k, v in (carry.get("recent_catalysts") or {}).items()
              if str(v) >= (date.fromisoformat(day) - timedelta(days=RECENT_CATALYST_DAYS)).isoformat()}
    return {"day": day, "events": [], "alerted": [], "sends": [], "sent_today": 0,
            "recent_catalysts": dict(sorted(recent.items(), key=lambda kv: kv[1])[-MAX_WATCH:]),
            "upcoming": prune_upcoming(carry.get("upcoming") or [], date.fromisoformat(day))}


def catalyst_kind(ev: dict[str, Any], text: str = "") -> str | None:
    """Biotech catalyst kind for one News Desk event, or None."""
    kind = str(ev.get("type") or "")
    blob = f"{ev.get('headline') or ''} {text or ''}"
    if kind in {"FDA_APPROVAL", "FDA_CRL", "TRIAL_FAIL", "TRIAL_DATA"}:
        return kind
    if _PDUFA.search(blob):
        return "PDUFA"
    if _ADCOM.search(blob):
        return "ADCOM"
    if _TOPLINE.search(str(ev.get("headline") or "")):
        return "TOPLINE"
    if kind == "FDA_OTHER":
        return kind
    return None


def catalyst_date(text: str, kind: str) -> str | None:
    """Best-effort scheduled date ("PDUFA date of March 3, 2027") from the release text."""
    if not text:
        return None
    anchor = {"PDUFA": _PDUFA, "ADCOM": _ADCOM, "TOPLINE": _TOPLINE}.get(kind)
    if anchor is None:
        return None
    m = anchor.search(text)
    if not m:
        return None
    window = text[m.start(): m.start() + 220]
    d = _DATE.search(window) or _QUARTER.search(window)
    return re.sub(r"\s+", " ", d.group(1)).strip() if d else None


def bio_snippet(headline: str, text: str, limit: int = 600) -> str | None:
    """Short text kept on a News Desk event so the catalyst date can be read later."""
    blob = text or ""
    for rx in (_PDUFA, _ADCOM, _TOPLINE):
        m = rx.search(blob)
        if m:
            start = max(0, m.start() - 80)
            return blob[start: start + limit]
    if _PDUFA.search(headline or "") or _ADCOM.search(headline or "") or _TOPLINE.search(headline or ""):
        return (blob or "")[:limit] or None
    return None


def quote_view(quote: dict[str, Any] | None) -> dict[str, Any]:
    q = quote or {}
    last = _num(q.get("last")) or _num(q.get("close"))
    prev = _num(q.get("prevclose"))
    chg = _num(q.get("change_percentage"))
    if chg is None and last and prev:
        chg = (last / prev - 1.0) * 100.0
    vol = _num(q.get("volume"))
    avg = _num(q.get("average_volume"))
    vol_x = (vol / avg) if vol and avg and avg > 0 else None
    return {"price": last, "change_pct": None if chg is None else round(chg, 2),
            "volume": vol, "avg_volume": avg, "vol_x": None if vol_x is None else round(vol_x, 1)}


def is_spike(view: dict[str, Any]) -> bool:
    price, chg, vx = view.get("price"), view.get("change_pct"), view.get("vol_x")
    return bool(price and price >= MIN_PRICE and chg is not None and abs(chg) >= SPIKE_MOVE_PCT
                and vx is not None and vx >= SPIKE_VOL_X)


def move_label(view: dict[str, Any]) -> str:
    bits = []
    if view.get("price"):
        bits.append(f"${float(view['price']):.2f}")
    if view.get("change_pct") is not None:
        bits.append(f"{float(view['change_pct']):+.1f}% today")
    if view.get("volume"):
        v = float(view["volume"])
        vs = f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K"
        bits.append(f"vol {vs}" + (f" ({float(view['vol_x']):.1f}x avg)" if view.get("vol_x") is not None else ""))
    return " · ".join(bits) or "no quote"


# ================================================================= upcoming-catalyst calendar

_Q_WORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1q": 1, "2q": 2, "3q": 3, "4q": 4,
           "q1": 1, "q2": 2, "q3": 3, "q4": 4}


def date_bound(text: str | None) -> date | None:
    """Latest calendar day a catalyst date string can mean ("March 3, 2027", "Q4 2026", "mid-2027")."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = re.search(r"(" + _MONTH + r")\s+(\d{1,2}),?\s+(20\d\d)", raw)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
        except ValueError:
            pass
    year = re.search(r"(20\d\d)", raw)
    if not year:
        return None
    y = int(year.group(1))
    low = raw.lower()
    if re.search(r"first half|\b1h\b|\bh1\b", low):
        return date(y, 6, 30)
    if re.search(r"second half|\b2h\b|\bh2\b|year[- ]end|end of", low):
        return date(y, 12, 31)
    for word, q in _Q_WORD.items():
        if re.search(r"\b" + word + r"\b", low) and ("quarter" in low or word.startswith("q") or word.endswith("q")):
            month = q * 3
            return date(y + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    if re.search(r"early|mid", low):
        return date(y, 4, 30) if "early" in low else date(y, 7, 31)
    m = re.search(r"(" + _MONTH + r")\s+(20\d\d)", raw)
    if m:
        try:
            first = datetime.strptime(f"{m.group(1)[:3]} 1 {m.group(2)}", "%b %d %Y").date()
            return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        except ValueError:
            pass
    return date(y, 12, 31)


def prune_upcoming(rows: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    keep: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("ticker"):
            continue
        bound = date_bound(r.get("date"))
        if bound is not None:
            if bound < today - timedelta(days=2):
                continue
        else:
            try:
                if date.fromisoformat(str(r.get("seen"))) < today - timedelta(days=UPCOMING_UNDATED_DAYS):
                    continue
            except ValueError:
                continue
        keep.append(r)
    keep.sort(key=lambda r: (date_bound(r.get("date")) or date.max, str(r.get("ticker"))))
    return keep[:MAX_UPCOMING]


def add_upcoming(state: dict[str, Any], row: dict[str, Any], today: date) -> None:
    rows = [r for r in state.get("upcoming") or []
            if not (r.get("ticker") == row["ticker"] and r.get("label") == row["label"])]
    rows.append(row)
    state["upcoming"] = prune_upcoming(rows, today)


def seed_upcoming(state: dict[str, Any], seeds: Iterable[dict[str, Any]], today: date) -> None:
    """Config-listed scheduled catalysts (company-guided dates); never overwrite a learned row."""
    have = {(r.get("ticker"), r.get("label")) for r in state.get("upcoming") or []}
    for s in seeds or []:
        if not isinstance(s, dict) or not s.get("ticker") or not s.get("label") or not s.get("date"):
            continue
        tk = str(s["ticker"]).upper()
        if (tk, str(s["label"])) in have:
            continue
        add_upcoming(state, {"ticker": tk, "label": str(s["label"])[:80], "date": str(s["date"])[:40],
                             "source": str(s.get("source") or "config")[:200], "seen": today.isoformat()}, today)


def upcoming_for(state: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    return [r for r in state.get("upcoming") or [] if r.get("ticker") == str(ticker).upper()]


def upcoming_line(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    return "Upcoming: " + "; ".join(f"{r['label']} — expected {r['date']}" for r in rows[:3])


# ================================================================= dilution (EDGAR submissions, cached)


class DilutionBook:
    """Recent shelf / prospectus filings per ticker. Small TTL cache, capped lookups per cycle."""

    def __init__(self, fetch: Callable[[int], dict[str, Any]] | None = None) -> None:
        self._fetch = fetch or _fetch_submissions
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def check(self, ticker: str, cik: int | None, today: date, budget: list[int],
              offering_today: bool = False) -> dict[str, Any]:
        tk = str(ticker or "").upper()
        hit = self._rows.get(tk)
        if hit and time.monotonic() - hit[0] < DILUTION_TTL_SEC:
            out = dict(hit[1])
        elif cik is None:
            out = {"status": "unknown", "detail": "no CIK"}
        elif budget[0] <= 0:
            return {"status": "unknown", "detail": "lookup deferred"}
        else:
            budget[0] -= 1
            try:
                out = recent_dilution(self._fetch(int(cik)), today)
            except Exception as exc:  # noqa: BLE001
                out = {"status": "unknown", "detail": f"EDGAR lookup failed ({str(exc)[:60]})"}
            self._rows[tk] = (time.monotonic(), out)
            if len(self._rows) > DILUTION_CACHE_MAX:
                for k, _ in sorted(self._rows.items(), key=lambda kv: kv[1][0])[: len(self._rows) - DILUTION_CACHE_MAX]:
                    self._rows.pop(k, None)
        if offering_today:
            out = {"status": "warn", "detail": "offering announced today (News Desk)"
                   + (f"; {out['detail']}" if out.get("status") == "warn" else "")}
        return out


def _fetch_submissions(cik: int) -> dict[str, Any]:
    from odte_scanner.news import feeds as F

    raw = F._sec_get(SUBMISSIONS_URL.format(cik=cik), max_bytes=SUBMISSIONS_MAX_BYTES, timeout=15.0)
    blob = json.loads(raw.decode("utf-8", "replace"))
    recent = ((blob.get("filings") or {}).get("recent") or {})
    # Keep only the two columns we need; drop the rest of the payload at once.
    return {"form": list(recent.get("form") or [])[:300], "filingDate": list(recent.get("filingDate") or [])[:300]}


def recent_dilution(sub: dict[str, Any], today: date, days: int = DILUTION_LOOKBACK_DAYS) -> dict[str, Any]:
    forms = sub.get("form") or []
    dates = sub.get("filingDate") or []
    cutoff = (today - timedelta(days=days)).isoformat()
    hits: list[str] = []
    for form, filed in zip(forms, dates):
        if str(filed) < cutoff:
            continue
        f = str(form).upper()
        if f in DILUTION_FORMS:
            hits.append(f"{f} {str(filed)[5:]}")
    if hits:
        return {"status": "warn", "detail": "recent shelf/offering filings: " + ", ".join(hits[:4])}
    return {"status": "clear", "detail": f"no S-3/S-1/424B in {days}d (EDGAR)"}


# ================================================================= runtime


class Runtime:
    def __init__(self) -> None:
        self.dilution = DilutionBook()
        self.last_spike_mono = 0.0
        self.last_send_mono = 0.0
        self.watchlist: tuple[str, ...] = DEFAULT_WATCHLIST
        self.upcoming_seeds: tuple[dict[str, Any], ...] = ()


RUNTIME = Runtime()


def load_watchlist(config_path: str | None = None) -> tuple[str, ...]:
    try:
        from odte_scanner.config import load_config

        cfg = load_config(config_path)
        raw = ((cfg.get("actions") or {}).get("biotech_watchlist")) or list(DEFAULT_WATCHLIST)
        out = tuple(dict.fromkeys(str(s).upper().strip() for s in raw if str(s).strip()))
        return out[:MAX_WATCH] or DEFAULT_WATCHLIST
    except Exception:  # noqa: BLE001
        return DEFAULT_WATCHLIST


def load_upcoming_seeds(config_path: str | None = None) -> tuple[dict[str, Any], ...]:
    try:
        from odte_scanner.config import load_config

        cfg = load_config(config_path)
        raw = ((cfg.get("actions") or {}).get("biotech_upcoming")) or []
        return tuple(r for r in raw if isinstance(r, dict))[:MAX_UPCOMING]
    except Exception:  # noqa: BLE001
        return ()


def telegram_enabled() -> bool:
    import os

    raw = (os.environ.get("BIOTECH_WATCH_TELEGRAM") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    from odte_scanner.news.desk import telegram_enabled as desk_tg

    return desk_tg()


def quiet(moment: datetime) -> bool:
    local = _ct(moment)
    return local.weekday() >= 5 or not (SEND_START_CT <= local.time() <= SEND_END_CT)


def _rth(moment: datetime) -> bool:
    local = _ct(moment)
    return local.weekday() < 5 and dtime(8, 30) <= local.time() <= dtime(15, 0)


# ================================================================= alert


def format_alert(row: dict[str, Any]) -> str:
    head = f"🧬 BIOTECH · {row.get('ticker')} · {row.get('label')}"
    lines = [head]
    if row.get("headline"):
        from odte_scanner.news.classify import display_headline

        lines.append(display_headline(str(row["headline"])))
    when = row.get("catalyst_date") or row.get("published_ct") or row.get("seen_ct")
    lines.append(f"Catalyst: {row.get('label')} · {when or 'today'}")
    lines.append(f"Move: {row.get('move_label') or 'no quote'}")
    dil = row.get("dilution") or {}
    icon = "⚠️ Dilution" if dil.get("status") == "warn" else ("Dilution" if dil.get("status") == "clear" else "Dilution ?")
    lines.append(f"{icon}: {dil.get('detail') or 'unknown'}")
    if row.get("upcoming_line"):
        lines.append(str(row["upcoming_line"]))
    lines.append(f"Playbook: {NOTE}")
    link = str(row.get("url") or "").strip()
    lines.append(f"{link} · alert only — no orders" if link else "alert only — no orders")
    return "\n".join(lines).replace("Sure Winner", "Gold Setup").replace("sure winner", "Gold Setup")


def _rate_ok(state: dict[str, Any], moment: datetime) -> tuple[bool, str]:
    now_ts = _ct(moment).timestamp()
    sends = [float(t) for t in state.get("sends") or [] if now_ts - float(t) < 3600]
    state["sends"] = sends
    if int(state.get("sent_today") or 0) >= SEND_MAX_PER_DAY:
        return False, f"daily cap {SEND_MAX_PER_DAY} reached"
    if len(sends) >= SEND_MAX_PER_HOUR:
        return False, f"hourly cap {SEND_MAX_PER_HOUR} reached"
    return True, ""


def _deliver(row: dict[str, Any], state: dict[str, Any], moment: datetime, rt: Runtime,
             send: Callable[[str], dict[str, Any]] | None, telegram: bool | None) -> None:
    key = f"{row['ticker']}|{row['kind']}"
    if key in set(state.get("alerted") or []):
        row["sent"], row["why_not_sent"] = False, "duplicate (already alerted today)"
        return
    if quiet(moment):
        row["sent"], row["why_not_sent"] = False, "quiet hours: recorded, not sent"
        return
    if not (telegram if telegram is not None else telegram_enabled()):
        row["sent"], row["why_not_sent"] = False, "telegram off"
        return
    ok, why = _rate_ok(state, moment)
    if not ok:
        row["sent"], row["why_not_sent"] = False, f"rate limit: {why}"
        return
    state.setdefault("alerted", []).append(key)  # mark first: a crash can never repeat it
    gap = 4.0 - (time.monotonic() - rt.last_send_mono)
    if gap > 0 and send is None:
        time.sleep(min(gap, 4.0))
    try:
        if send is None:
            from odte_scanner.alerts.telegram import send_telegram_text

            res = send_telegram_text(format_alert(row), timeout=10.0) or {}
        else:
            res = send(format_alert(row)) or {}
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)[:160]}
    rt.last_send_mono = time.monotonic()
    row["sent"] = bool(res.get("ok"))
    if row["sent"]:
        state.setdefault("sends", []).append(_ct(moment).timestamp())
        state["sent_today"] = int(state.get("sent_today") or 0) + 1
    else:
        row["why_not_sent"] = str(res.get("error") or "send failed")[:160]


# ================================================================= one pass


def run_pass(
    events: Iterable[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    moment: datetime,
    *,
    quote_fn: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
    cik_fn: Callable[[str], int | None] | None = None,
    store_path: Path | None = None,
    rt: Runtime | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    telegram: bool | None = None,
    force_spike: bool = False,
) -> dict[str, Any]:
    """Catalyst events from this News Desk cycle + a throttled volume-spike sweep. Never orders."""
    rt = rt or RUNTIME
    local = _ct(moment)
    day = local.date().isoformat()
    state = read_state(store_path)
    if str(state.get("day") or "") != day:
        state = _fresh(day, carry=state)
    seed_upcoming(state, rt.upcoming_seeds, local.date())
    budget = [DILUTION_LOOKUPS_PER_CYCLE]
    offering_today = {str(e.get("ticker") or "").upper() for e in state.get("desk_offerings") or []}
    rows: list[dict[str, Any]] = []
    try:
        evs = list(events or [])
        for ev in evs:
            if ev.get("type") in {"OFFERING"} and ev.get("ticker"):
                offering_today.add(str(ev["ticker"]).upper())
                state.setdefault("desk_offerings", []).append({"ticker": ev["ticker"]})
                state["desk_offerings"] = state["desk_offerings"][-50:]
        for ev in evs:
            tk = str(ev.get("ticker") or "").upper()
            text = str(ev.pop("bio_text", None) or "")
            if not tk or ev.get("stale"):
                continue
            if ev.get("sent"):
                continue  # liquid name the News Desk already alerted
            kind = catalyst_kind(ev, text)
            if not kind:
                continue
            q = quotes.get(tk)
            if q is None and quote_fn is not None:
                q = (quote_fn([tk]) or {}).get(tk)
            view = quote_view(q)
            if view.get("price") is not None and view["price"] < MIN_PRICE:
                continue
            rows.append({
                "ticker": tk, "kind": kind, "label": KIND_LABELS.get(kind, kind),
                "headline": str(ev.get("headline") or "")[:220], "url": ev.get("url"),
                "published_ct": ev.get("published_ct"), "catalyst_date": catalyst_date(text, kind),
                **view, "move_label": move_label(view), "seen_ct": local.strftime("%H:%M"),
                "_cik": ev.get("cik"),
            })
            state.setdefault("recent_catalysts", {})[tk] = day
            when = rows[-1]["catalyst_date"]
            if kind in {"PDUFA", "ADCOM", "TOPLINE"} and when:
                bound = date_bound(when)
                if bound is None or bound >= local.date():
                    add_upcoming(state, {"ticker": tk, "label": KIND_LABELS.get(kind, kind), "date": when,
                                         "source": str(ev.get("url") or "")[:200], "seen": day}, local.date())
        # Volume spikes on the watchlist + names with a catalyst in the last 10 days.
        mono = time.monotonic()
        if force_spike or (_rth(moment) and mono - rt.last_spike_mono >= SPIKE_EVERY_SEC):
            rt.last_spike_mono = mono
            watch = list(dict.fromkeys([*rt.watchlist, *sorted(state.get("recent_catalysts") or {})]))[:MAX_WATCH]
            got = (quote_fn(watch) if quote_fn else {}) or {}
            for tk in watch:
                view = quote_view(got.get(tk))
                if not is_spike(view):
                    continue
                rows.append({
                    "ticker": tk, "kind": "VOLUME_SPIKE",
                    "label": f"Volume spike {'up' if (view['change_pct'] or 0) > 0 else 'down'}",
                    "headline": None, "url": None, "published_ct": None, "catalyst_date": None,
                    **view, "move_label": move_label(view), "seen_ct": local.strftime("%H:%M"), "_cik": None,
                })
        for row in rows:
            cik = row.pop("_cik", None)
            if cik is None and cik_fn is not None:
                try:
                    cik = cik_fn(row["ticker"])
                except Exception:  # noqa: BLE001
                    cik = None
            row["dilution"] = rt.dilution.check(row["ticker"], cik, local.date(), budget,
                                                offering_today=row["ticker"] in offering_today)
            row["alert_only"] = True
            row["upcoming_line"] = upcoming_line(upcoming_for(state, row["ticker"]))
            _deliver(row, state, moment, rt, send, telegram)
            state.setdefault("events", []).append(row)
        state["events"] = state["events"][-MAX_EVENTS:]
        state["alerted"] = list(state.get("alerted") or [])[-400:]
        state["recent_catalysts"] = dict(sorted((state.get("recent_catalysts") or {}).items(),
                                                key=lambda kv: kv[1])[-MAX_WATCH:])
    finally:
        state["updated_at"] = local.isoformat()
        write_state(state, store_path)
    return {"ok": True, "rows": len(rows), "sent": sum(1 for r in rows if r.get("sent"))}


# ================================================================= snapshot block


def build_block(store_path: Path | None = None) -> dict[str, Any]:
    state = read_state(store_path)
    keys = ("ticker", "label", "kind", "headline", "url", "published_ct", "seen_ct", "catalyst_date", "price",
            "change_pct", "vol_x", "move_label", "dilution", "upcoming_line", "sent", "why_not_sent")
    events = [{k: e.get(k) for k in keys} for e in reversed(state.get("events") or [])][:30]
    return {
        "ok": True, "label": LABEL, "alert_only": True, "orders": False, "day": state.get("day"),
        "events": events, "n_events": len(state.get("events") or []), "sent_today": int(state.get("sent_today") or 0),
        "watchlist": list(RUNTIME.watchlist), "min_price": MIN_PRICE,
        "upcoming": [{k: r.get(k) for k in ("ticker", "label", "date", "source")}
                     for r in prune_upcoming(state.get("upcoming") or [], _ct().date())][:30],
        "spike_rule": f"≥{SPIKE_VOL_X:.0f}x avg volume and ≥{SPIKE_MOVE_PCT:.0f}% move, price ≥ ${MIN_PRICE:.0f}",
        "updated_at": state.get("updated_at"), "note": NOTE,
    }


def public_block() -> dict[str, Any]:
    try:
        return build_block()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "label": LABEL, "alert_only": True, "events": [], "error": str(exc)[:200],
                "note": "Biotech Catalyst Watch unavailable."}
