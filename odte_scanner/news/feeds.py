"""Public news feeds for the 📰 News Desk. Read only, bounded, fail soft.

Every fetch reads at most ``max_bytes`` and returns small dicts. A failing
source raises ``FeedError``; the desk records the error and keeps going with
the other sources.

SEC rules: a descriptive User-Agent on every request (override with the
``SEC_USER_AGENT`` env, e.g. "Your Name you@example.com") and well under
10 requests per second. The desk polls the 8-K Atom feed every 30–60 s.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CIK_MAP_STORE = ROOT / "outputs" / "news_cik_map.json"

EDGAR_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count={count}&output=atom"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
PRN_RSS = "https://www.prnewswire.com/rss/news-releases-list.rss"
FDA_RSS = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
POLYGON_NEWS = "https://api.polygon.io/v2/reference/news"

DEFAULT_SEC_UA = "stockscanner-news-desk/1.0 (+https://stockscanner.fly.dev)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

FEED_MAX_BYTES = 2_000_000
DOC_MAX_BYTES = 150_000
TEXT_KEEP = 5000
SEC_MIN_GAP_SEC = 0.25  # at most ~4 SEC requests per second from this desk
LISTED = {"NYSE", "NASDAQ", "NYSE AMERICAN", "CBOE", "NYSE ARCA"}

_SEC_LOCK = threading.Lock()
_SEC_LAST = [0.0]


class FeedError(RuntimeError):
    """A source failed (network, HTTP status, parse). The desk keeps going."""


class FeedAuthError(FeedError):
    """A keyed source rejected the key. The desk turns that source off for the day."""


def sec_user_agent() -> str:
    return (os.environ.get("SEC_USER_AGENT") or "").strip() or DEFAULT_SEC_UA


def _get(url: str, *, headers: dict[str, str], timeout: float = 10.0, max_bytes: int = FEED_MAX_BYTES,
         params: dict[str, Any] | None = None) -> bytes:
    """GET at most ``max_bytes``. Raises FeedError on any failure."""
    try:
        with requests.get(url, headers=headers, timeout=timeout, stream=True, params=params) as resp:
            if resp.status_code in (401, 403) and "polygon" in url:
                raise FeedAuthError(f"HTTP {resp.status_code}")
            if resp.status_code == 429:
                raise FeedError("HTTP 429 (rate limited)")
            if resp.status_code != 200:
                raise FeedError(f"HTTP {resp.status_code}")
            chunks: list[bytes] = []
            size = 0
            for chunk in resp.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            return b"".join(chunks)[:max_bytes]
    except FeedError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeedError(redact_error(str(exc))) from exc


_SECRET_PARAM = re.compile(r"(?i)(apikey|api_key|token|key)=[^&\s'\"]+")


def redact_error(text: str) -> str:
    """Error text safe for logs and the portal: no query-string keys, short."""
    return _SECRET_PARAM.sub(r"\1=***", str(text or ""))[:200]


def _sec_get(url: str, *, max_bytes: int = FEED_MAX_BYTES, timeout: float = 10.0) -> bytes:
    with _SEC_LOCK:
        wait = SEC_MIN_GAP_SEC - (time.monotonic() - _SEC_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _SEC_LAST[0] = time.monotonic()
    return _get(url, headers={"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"},
                timeout=timeout, max_bytes=max_bytes)


def clean_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|ix:header)\b.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<div[^>]*display:\s*none[^>]*>.*?</div>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            # FDA writes "EST"/"EDT" which email.utils already handles; anything else is unknown
            return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _xml(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedError(f"bad XML: {exc}") from exc


# ------------------------------------------------------------------ EDGAR

_ATOM = "{http://www.w3.org/2005/Atom}"
_ADSH = re.compile(r"/data/(\d+)/(\d{18})/")
_TITLE = re.compile(r"^(?P<form>[\w\-/]+) - (?P<name>.*?) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)")


def parse_edgar_atom(raw: bytes) -> list[dict[str, Any]]:
    root = _xml(raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in root.findall(f"{_ATOM}entry"):
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        summary = html.unescape(entry.findtext(f"{_ATOM}summary") or "")
        match = _TITLE.match(title)
        adsh_m = _ADSH.search(link or "")
        if not match or not adsh_m:
            continue
        form = match.group("form").upper()
        if form.endswith("/A"):
            continue  # amendments repeat old news
        adsh = adsh_m.group(2)
        if adsh in seen:
            continue
        seen.add(adsh)
        items = re.findall(r"Item (\d\.\d\d)", summary)
        out.append(
            {
                "source": "edgar",
                "id": f"edgar:{adsh}",
                "form": form,
                "cik": int(match.group("cik")),
                "company": match.group("name").strip(),
                "items": items,
                "published": _dt(entry.findtext(f"{_ATOM}updated")),
                "url": link,
                "title": title[:200],
            }
        )
    return out


def fetch_edgar(form: str = "8-K", count: int = 100) -> list[dict[str, Any]]:
    return parse_edgar_atom(_sec_get(EDGAR_ATOM.format(form=form, count=int(count))))


_SUBMISSION_FILE = re.compile(r"^\d{10}-\d{2}-\d{6}")


def fetch_edgar_text(cik: int, adsh: str) -> str:
    """Press-release text (EX-99.x, else the primary doc). At most TEXT_KEEP chars."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh}/"
    raw = _sec_get(base + "index.json", max_bytes=300_000)
    try:
        listing = json.loads(raw.decode("utf-8", "replace"))
        names = [str(i.get("name") or "") for i in (listing.get("directory") or {}).get("item") or []]
    except (ValueError, AttributeError) as exc:
        raise FeedError(f"bad index.json: {exc}") from exc
    docs = [
        n
        for n in names
        if n.lower().endswith((".htm", ".html", ".txt"))
        and "index" not in n.lower()
        and not _SUBMISSION_FILE.match(n)  # the full SGML submission, not a document
        and not re.match(r"(?i)^(r\d+\.htm|filingsummary)", n)  # XBRL viewer pages
    ]
    docs.sort(key=lambda n: (not n.lower().endswith((".htm", ".html")),))
    exhibits = [n for n in docs if re.search(r"ex[-_]?99|ex991|dex99|99[-_.]?1", n.lower())]
    pick = exhibits[0] if exhibits else (docs[0] if docs else None)
    if not pick:
        return ""
    body = _sec_get(base + pick, max_bytes=DOC_MAX_BYTES).decode("utf-8", "replace")
    return clean_html(body)[:TEXT_KEEP]


# ------------------------------------------------------------------ wires

_TICKER = re.compile(
    r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSE MKT|Cboe|CBOE|Nasdaq GS|Nasdaq GM|Nasdaq CM)"
    r"(?:\s*:\s*|\s+)([A-Z]{1,5}(?:\.[A-Z])?)\s*(?:[;,][^)]*)?\)"
)


def tickers_in(text: str) -> list[str]:
    found: list[str] = []
    for sym in _TICKER.findall(text or ""):
        sym = sym.replace(".", "-").upper()
        if sym not in found:
            found.append(sym)
    return found[:4]


def parse_rss(raw: bytes, source: str) -> list[dict[str, Any]]:
    root = _xml(raw)
    out: list[dict[str, Any]] = []
    for item in root.iter("item"):
        title = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        desc = clean_html(item.findtext("description") or "")[:800]
        subjects = [str(c.text or "").strip() for c in item if c.tag.endswith("subject") and c.text]
        if not title or not guid:
            continue
        out.append(
            {
                "source": source,
                "id": f"{source}:{guid[-160:]}",
                "title": title[:300],
                "text": desc,
                "url": link,
                "published": _dt(item.findtext("pubDate")),
                "tickers": tickers_in(title + " " + desc),
                "subjects": subjects[:12],
            }
        )
    return out[:100]


def fetch_prn() -> list[dict[str, Any]]:
    return parse_rss(_get(PRN_RSS, headers={"User-Agent": BROWSER_UA}), "prnewswire")


def fetch_fda() -> list[dict[str, Any]]:
    return parse_rss(_get(FDA_RSS, headers={"User-Agent": BROWSER_UA}), "fda")


# ------------------------------------------------------------------ Nasdaq earnings


def _money(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw or raw.upper() in {"N/A", "--"}:
        return None
    neg = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").replace("$", "").replace(",", "")
    try:
        number = float(raw)
    except ValueError:
        return None
    return -number if neg else number


def parse_nasdaq_earnings(raw: bytes) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedError(f"bad JSON: {exc}") from exc
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows[:2000]:
        sym = str(row.get("symbol") or "").upper().replace(".", "-")
        if not sym:
            continue
        surprise = None
        try:
            if row.get("surprise") not in (None, "", "N/A"):
                surprise = float(str(row.get("surprise")).replace("%", ""))
        except ValueError:
            surprise = None
        out[sym] = {
            "eps": _money(row.get("eps")),
            "forecast": _money(row.get("epsForecast")),
            "surprise_pct": surprise,
            "time": str(row.get("time") or "")[:30],
        }
    return out


def fetch_nasdaq_earnings(day: str) -> dict[str, dict[str, Any]]:
    raw = _get(NASDAQ_EARNINGS.format(day=day), headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
               max_bytes=1_500_000)
    return parse_nasdaq_earnings(raw)


# ------------------------------------------------------------------ Polygon / Massive (optional)


def polygon_key() -> str | None:
    for name in ("POLYGON_API_KEY", "MASSIVE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def fetch_polygon_news(limit: int = 50) -> list[dict[str, Any]]:
    key = polygon_key()
    if not key:
        return []
    # The key goes in a header, never the URL, so an error string cannot leak it.
    raw = _get(
        POLYGON_NEWS,
        headers={"User-Agent": BROWSER_UA, "Authorization": f"Bearer {key}"},
        params={"limit": int(limit), "order": "desc", "sort": "published_utc"},
        max_bytes=1_500_000,
    )
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise FeedError(f"bad JSON: {exc}") from exc
    out: list[dict[str, Any]] = []
    for row in (payload.get("results") or [])[:limit]:
        title = str(row.get("title") or "").strip()
        tickers = [str(t).upper() for t in (row.get("tickers") or [])]
        if not title or not tickers or len(tickers) > 2:
            continue  # market round-ups tag many names; only single-company stories count
        out.append(
            {
                "source": "polygon",
                "id": f"polygon:{row.get('id')}",
                "title": title[:300],
                "text": str(row.get("description") or "")[:800],
                "url": str(row.get("article_url") or ""),
                "published": _dt(row.get("published_utc")),
                "tickers": tickers,
                "subjects": [],
            }
        )
    return out


# ------------------------------------------------------------------ CIK -> ticker map


_COMMON_TICKER = re.compile(r"^[A-Z]{1,5}(-[A-C])?$")


class CikMap:
    """CIK -> ticker and ticker -> company name for listed US names (~6k rows, ~2 MB)."""

    MAX_AGE_SEC = 7 * 86400

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CIK_MAP_STORE
        self.cik_to_ticker: dict[int, str] = {}
        self.names: dict[str, str] = {}
        self.loaded_at = 0.0

    def _from_rows(self, rows: list[list[Any]]) -> None:
        ciks: dict[int, str] = {}
        names: dict[str, str] = {}
        for row in rows:
            try:
                cik, name, ticker, exch = row[0], row[1], row[2], row[3]
            except (IndexError, TypeError):
                continue
            if not ticker or str(exch or "").upper() not in LISTED:
                continue
            ticker = str(ticker).upper().replace(".", "-")
            if not _COMMON_TICKER.match(ticker):
                continue  # units, warrants, rights, preferreds
            ciks.setdefault(int(cik), ticker)  # first listed class wins
            names[ticker] = str(name or "")[:80]
        self.cik_to_ticker = ciks
        self.names = names

    def ensure(self, *, fetch: bool = True) -> bool:
        if self.cik_to_ticker and time.time() - self.loaded_at < self.MAX_AGE_SEC:
            return True
        rows = None
        try:
            if self.path.exists() and time.time() - self.path.stat().st_mtime < self.MAX_AGE_SEC:
                rows = json.loads(self.path.read_text(encoding="utf-8")).get("data")
        except (OSError, ValueError):
            rows = None
        if rows is None and fetch:
            try:
                raw = _sec_get(EDGAR_TICKERS, max_bytes=5_000_000, timeout=20.0)
                blob = json.loads(raw.decode("utf-8", "replace"))
                rows = [r for r in blob.get("data") or [] if isinstance(r, list) and len(r) >= 4]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"data": rows}), encoding="utf-8")
            except (FeedError, OSError, ValueError) as exc:
                logger.info("news desk cik map fetch failed: %s", exc)
                rows = None
        if not rows:
            return bool(self.cik_to_ticker)
        self._from_rows(rows)
        self.loaded_at = time.time()
        return True

    def ticker(self, cik: Any) -> str | None:
        try:
            return self.cik_to_ticker.get(int(cik))
        except (TypeError, ValueError):
            return None

    def name(self, ticker: str | None) -> str | None:
        return self.names.get(str(ticker or "").upper())
