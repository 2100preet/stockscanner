"""Pre-market levels card for SPY, QQQ, and SPX. Information only.

EM (expected move) = prior close × vol index / 100 / sqrt(252). VIX is used for
SPY and SPX, VXN for QQQ. The band is ±EM around the prior close, the shelves
are ±½ EM, and the week corridor is ±EM×sqrt(5) from the last close before the
week started. The regime is FAST when the prior VIX close is above 18.5.

Base rates come from the Nov 2026 expected-move study (2021-2026 daily bars,
SPY / QQQ standing in for the SPX / NDX cash CFDs). That study found no
tradable edge; the numbers describe how big a day tends to be, nothing more.
This card never places, sizes, or exits an order.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORE = ROOT / "outputs" / "levels_card.json"
FAST_VIX = 18.5
SQRT252 = math.sqrt(252.0)
SQRT5 = math.sqrt(5.0)
RETRY_SEC = 600.0
LABEL = "Pre-market levels · info only, not a signal"
NOTE = (
    "Information only — not a signal and never an order. EM = prior close × VIX "
    "(VXN for QQQ) / 100 / √252. Band = ±EM, shelves = ±½ EM, week corridor = "
    "±EM×√5 from the last close before the week. FAST = prior VIX close above 18.5. "
    "Base rates: 2021–2026 daily study (SPY/QQQ for the SPX/NDX cash CFDs); "
    "it found no tradable edge."
)

# (card symbol, Yahoo daily symbol, vol index, Yahoo vol symbol, study row)
INSTRUMENTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("SPX", "^GSPC", "VIX", "^VIX", "SPY"),
    ("SPY", "SPY", "VIX", "^VIX", "SPY"),
    ("QQQ", "QQQ", "VXN", "^VXN", "QQQ"),
)

# NYSE full-day closures we may hit while this card is in use.
MARKET_HOLIDAYS = frozenset(
    {
        "2026-11-26",
        "2026-12-25",
        "2027-01-01",
        "2027-01-18",
        "2027-02-15",
        "2027-03-26",
        "2027-05-31",
        "2027-06-18",
        "2027-07-05",
        "2027-09-06",
        "2027-11-25",
        "2027-12-24",
    }
)

# 8:30 AM ET releases from the BLS schedule (Employment Situation, CPI, PPI,
# plus the two other 8:30 BLS prints left in Dec 2026).
EVENTS_830_ET: dict[str, str] = {
    "2026-12-04": "Jobs report (NFP)",
    "2026-12-08": "Productivity & costs",
    "2026-12-10": "CPI",
    "2026-12-15": "PPI",
    "2026-12-17": "Import/export prices",
    "2027-01-08": "Jobs report (NFP)",
    "2027-01-13": "CPI",
    "2027-01-14": "PPI",
    "2027-02-05": "Jobs report (NFP)",
    "2027-02-11": "CPI",
    "2027-02-12": "PPI",
    "2027-03-05": "Jobs report (NFP)",
    "2027-03-10": "CPI",
    "2027-03-12": "PPI",
    "2027-04-02": "Jobs report (NFP)",
    "2027-04-13": "CPI",
    "2027-04-14": "PPI",
    "2027-05-07": "Jobs report (NFP)",
    "2027-05-12": "CPI",
    "2027-05-13": "PPI",
    "2027-06-04": "Jobs report (NFP)",
    "2027-06-10": "CPI",
    "2027-06-11": "PPI",
    "2027-07-02": "Jobs report (NFP)",
    "2027-07-14": "CPI",
    "2027-07-15": "PPI",
    "2027-08-06": "Jobs report (NFP)",
    "2027-08-10": "PPI",
    "2027-08-11": "CPI",
    "2027-09-03": "Jobs report (NFP)",
    "2027-09-10": "PPI",
    "2027-09-14": "CPI",
    "2027-10-08": "Jobs report (NFP)",
    "2027-10-14": "CPI",
    "2027-10-15": "PPI",
    "2027-11-05": "Jobs report (NFP)",
    "2027-11-10": "CPI",
    "2027-11-12": "PPI",
    "2027-12-03": "Jobs report (NFP)",
    "2027-12-10": "CPI",
    "2027-12-15": "PPI",
}

# Base rates from /workspace/em base_stats.csv. Keys are the study row groups.
_STAT_KEYS = (
    "days",
    "touch_shelf",
    "touch_band",
    "range_gt_em",
    "close_beyond_shelf",
    "close_beyond_band",
    "med_range_over_em",
    "share_range_by_1130",
    "extreme_by_1130",
)
_RAW_STATS: dict[str, dict[str, tuple[float, ...]]] = {
    "SPY": {
        "all": (1322, 0.776, 0.322, 0.314, 0.474, 0.182, 0.828, 0.688, 0.157),
        "fast": (614, 0.863, 0.399, 0.414, 0.552, 0.225, 0.922, 0.679, 0.129),
        "calm": (708, 0.701, 0.256, 0.227, 0.405, 0.144, 0.745, 0.696, 0.181),
        "event": (237, 0.823, 0.384, 0.359, 0.549, 0.232, 0.867, 0.708, 0.169),
        "fast_event": (109, 0.844, 0.440, 0.440, 0.569, 0.284, 0.918, 0.696, 0.165),
    },
    "QQQ": {
        "all": (1320, 0.842, 0.408, 0.433, 0.533, 0.236, 0.938, 0.742, 0.200),
        "fast": (611, 0.908, 0.466, 0.530, 0.592, 0.280, 1.020, 0.724, 0.167),
        "calm": (709, 0.784, 0.357, 0.348, 0.481, 0.199, 0.864, 0.758, 0.228),
        "event": (237, 0.873, 0.443, 0.477, 0.578, 0.283, 0.953, 0.749, 0.190),
        "fast_event": (109, 0.890, 0.523, 0.514, 0.596, 0.321, 1.034, 0.732, 0.165),
    },
}
_GROUP_LABEL = {
    "all": "all days",
    "fast": "FAST days (VIX > 18.5)",
    "calm": "calm days",
    "event": "8:30 ET event days",
    "fast_event": "FAST event days",
}

_lock = threading.Lock()
_memo: dict[str, Any] = {"card": None, "at": 0.0, "tried": 0.0}


def base_stats(row: str, group: str) -> dict[str, Any]:
    raw = _RAW_STATS[row][group]
    out: dict[str, Any] = dict(zip(_STAT_KEYS, raw))
    out["days"] = int(out["days"])
    out["group"] = group
    out["group_label"] = _GROUP_LABEL[group]
    return out


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in MARKET_HOLIDAYS


def session_for(now: datetime | None = None) -> date:
    """Today's session until the 4:00 PM ET close, then the next one."""
    moment = (now or datetime.now(timezone.utc)).astimezone(ET)
    day = moment.date()
    if is_session(day) and moment.hour < 16:
        return day
    day += timedelta(days=1)
    while not is_session(day):
        day += timedelta(days=1)
    return day


def week_start(session: date) -> date:
    """First session of the week that holds ``session``."""
    monday = session - timedelta(days=session.weekday())
    day = monday
    while not is_session(day) and day < session:
        day += timedelta(days=1)
    return day


def event_for(session: date) -> dict[str, str] | None:
    name = EVENTS_830_ET.get(session.isoformat())
    if not name:
        return None
    return {"name": name, "time": "8:30 AM ET (7:30 AM CT)"}


def last_close_before(bars: list[tuple[str, float]], day: date) -> tuple[str, float] | None:
    cutoff = day.isoformat()
    found = None
    for bar_day, close in sorted(bars):
        if bar_day < cutoff and close and close > 0:
            found = (bar_day, float(close))
    return found


def _em(close: float, vol: float) -> float:
    return close * vol / 100.0 / SQRT252


def _round(symbol: str, value: float) -> float:
    return round(value, 2)


def _stat_group(fast: bool, event: bool) -> str:
    if fast and event:
        return "fast_event"
    if event:
        return "event"
    return "fast" if fast else "calm"


def stats_line(stats: dict[str, Any]) -> str:
    def pct(key: str) -> str:
        return f"{round(100 * float(stats[key]))}%"

    return (
        f"On {stats['group_label']} (n={stats['days']}): range > EM {pct('range_gt_em')} · "
        f"touches ½-EM shelf {pct('touch_shelf')} · touches band {pct('touch_band')} · "
        f"closes beyond band {pct('close_beyond_band')} · "
        f"{pct('share_range_by_1130')} of the day's range is set by 11:30 ET"
    )


def compute_card(
    bars: dict[str, list[tuple[str, float]]],
    *,
    session: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pure math from daily closes keyed by Yahoo symbol."""
    moment = now or datetime.now(timezone.utc)
    vix_prior = last_close_before(bars.get("^VIX") or [], session)
    fast = bool(vix_prior and vix_prior[1] > FAST_VIX)
    event = event_for(session)
    group = _stat_group(fast, event is not None)
    week_day = week_start(session)
    rows: list[dict[str, Any]] = []
    for symbol, yahoo, vol_name, vol_yahoo, study in INSTRUMENTS:
        prior = last_close_before(bars.get(yahoo) or [], session)
        vol = last_close_before(bars.get(vol_yahoo) or [], session)
        if not prior or not vol:
            rows.append({"symbol": symbol, "ok": False, "vol_name": vol_name, "note": "close or vol index missing"})
            continue
        close = prior[1]
        em = _em(close, vol[1])
        stats = base_stats(study, group)
        row: dict[str, Any] = {
            "symbol": symbol,
            "ok": True,
            "prior_close": _round(symbol, close),
            "prior_close_day": prior[0],
            "vol_name": vol_name,
            "vol": round(vol[1], 2),
            "em": _round(symbol, em),
            "em_pct": round(100 * em / close, 2),
            "band": [_round(symbol, close - em), _round(symbol, close + em)],
            "shelves": [_round(symbol, close - em / 2), _round(symbol, close + em / 2)],
            "study_row": f"{study} ({'SPX' if study == 'SPY' else 'NDX'} CFD)",
            "stats": stats,
            "stats_line": stats_line(stats),
        }
        week_close = last_close_before(bars.get(yahoo) or [], week_day)
        week_vol = last_close_before(bars.get(vol_yahoo) or [], week_day)
        if week_close and week_vol:
            wide = _em(week_close[1], week_vol[1]) * SQRT5
            row["week"] = {
                "anchor": _round(symbol, week_close[1]),
                "anchor_day": week_close[0],
                "half_width": _round(symbol, wide),
                "low": _round(symbol, week_close[1] - wide),
                "high": _round(symbol, week_close[1] + wide),
            }
        rows.append(row)
    ok = any(row.get("ok") for row in rows)
    return {
        "ok": ok,
        "label": LABEL,
        "info_only": True,
        "orders": False,
        "session": session.isoformat(),
        "regime": "FAST" if fast else "calm",
        "vix_prior": round(vix_prior[1], 2) if vix_prior else None,
        "vix_prior_day": vix_prior[0] if vix_prior else None,
        "fast_threshold": FAST_VIX,
        "event": event,
        "stat_group": group,
        "symbols": rows,
        "computed_at": moment.astimezone(CT).isoformat(timespec="seconds"),
        "source": "Yahoo daily closes",
        "note": NOTE,
    }


def fetch_daily_closes(yahoo_symbol: str, *, timeout: float = 5.0) -> list[tuple[str, float]]:
    """Daily closes keyed by New York trade date. Today's partial bar is kept;
    ``last_close_before`` drops it."""
    import requests

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1d&range=1mo"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    response.raise_for_status()
    result = ((response.json().get("chart") or {}).get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    closes = quote.get("close") or []
    out: list[tuple[str, float]] = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        try:
            day = datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(ET).date().isoformat()
            value = float(close)
        except (TypeError, ValueError, OverflowError):
            continue
        if value > 0:
            out.append((day, value))
    return out


def default_fetch() -> dict[str, list[tuple[str, float]]]:
    import os
    from concurrent.futures import ThreadPoolExecutor

    if "PYTEST_CURRENT_TEST" in os.environ:
        return {}
    symbols = sorted({row[1] for row in INSTRUMENTS} | {row[3] for row in INSTRUMENTS})
    out: dict[str, list[tuple[str, float]]] = {}

    def one(symbol: str) -> list[tuple[str, float]]:
        try:
            return fetch_daily_closes(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.info("levels card: %s daily closes failed: %s", symbol, exc)
            return []

    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        for symbol, bars in zip(symbols, pool.map(one, symbols)):
            if bars:
                out[symbol] = bars
    return out


def _read(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write(path: Path, card: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(card, indent=1))
        tmp.replace(path)
    except OSError as exc:
        logger.info("levels card write failed: %s", exc)


def levels_card(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    fetch: Callable[[], dict[str, list[tuple[str, float]]]] | None = None,
) -> dict[str, Any]:
    """The card for the next session. One fetch per session, retried every
    10 minutes until it lands. Cheap after that (memory, then disk)."""
    store = path or DEFAULT_STORE
    session = session_for(now).isoformat()
    clock = time.monotonic()
    with _lock:
        card = _memo.get("card")
        if path is None and isinstance(card, dict) and card.get("session") == session and card.get("ok"):
            return card
        disk = _read(store)
        if isinstance(disk, dict) and disk.get("session") == session and disk.get("ok"):
            if path is None:
                _memo["card"] = disk
            return disk
        if path is None and clock - float(_memo.get("tried") or 0) < RETRY_SEC and isinstance(card, dict):
            return card
        _memo["tried"] = clock
    try:
        bars = (fetch or default_fetch)() or {}
        fresh = compute_card(bars, session=date.fromisoformat(session), now=now)
    except Exception as exc:  # noqa: BLE001
        logger.info("levels card failed: %s", exc)
        fresh = {"ok": False, "label": LABEL, "info_only": True, "session": session, "symbols": [], "error": str(exc)[:200]}
    if fresh.get("ok"):
        _write(store, fresh)
    elif isinstance(disk, dict) and disk.get("ok"):
        fresh = {**disk, "stale": True}
    if path is None:
        with _lock:
            _memo["card"] = fresh
    return fresh


def public_block() -> dict[str, Any]:
    try:
        return levels_card()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "label": LABEL, "info_only": True, "symbols": [], "error": str(exc)[:200]}
