"""Squeeze Fire: Telegram alert only, plus a 30-day paper study.

The rule is the one backtested on Nov 16, 2026 (60 days of 5-minute bars,
121 focus names). On a CLOSED 5-minute bar:

* the TTM squeeze (Bollinger 20/2 inside Keltner 20/1.5) was on for at least
  6 bars in a row, and this bar it is off ("fires");
* squeeze momentum is above zero and rising;
* close > session VWAP, close > EMA9 > EMA21, close > upper Bollinger band;
* bar volume is at least 1.5x the prior 20 bars' average;
* the bar starts between 9:00 and 1:55 PM CT;
* at most one alert per name per day.

That backtest found NO edge (about 17% winners on modeled option prices), so
this desk never trades. It sends an "alert only" Telegram message with a
suggested near-the-money call and its live bid/ask, and logs that contract's
bid/ask at the signal, +15, +30, +60 minutes and 2:55 PM CT to a persistent
paper record so 30 days of real quotes can settle the question.

This module never imports the Webull broker or the auto-trader and never
places, sizes or exits an order. SPX and other index products are excluded.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
DAY_STORE = ROOT / "outputs" / "squeeze_fire_day.json"
STUDY_STORE = ROOT / "outputs" / "squeeze_fire_study.jsonl"

LABEL = "Squeeze Fire (alert only)"
NOTE = (
    "Alert only. Telegram message plus a paper record of the suggested call's "
    "bid/ask at the signal, +15/+30/+60 min and 2:55 PM CT, for a 30-day study. "
    "Never places an order. The Nov 16 backtest of this rule showed no edge."
)

# SPCX is the miss that started this. CRWV, NBIS, IREN, ASTS and CRCL were the
# widest-range focus names with liquid weeklies (Nov 16 backtest data). AVGO was
# added on request. MARA, RIOT, WULF, APLD, CBRS and LUNR were added from a
# Nov 16 ranking of 60 days of 5-minute bars on 148 names: median 12:00–3:00 PM
# CT range of 2.6–3.3%, top 30 in BOTH 30-day halves, and an at-the-money weekly
# call spread under 10% with open interest above 1,000. Per-name signal counts
# are thin (about 18 signals in 60 days across all 13), so this is a "moves a
# lot in the afternoon" list, not a proven edge.
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "SPCX", "CRWV", "NBIS", "IREN", "ASTS", "CRCL", "AVGO",
    "MARA", "RIOT", "WULF", "APLD", "CBRS", "LUNR",
)
EXCLUDED = frozenset({"SPX", "SPXW", "XSP", "NDX", "RUT", "VIX"})

BAR_MIN = 5
BB_LEN = 20
BB_MULT = 2.0
KC_MULT = 1.5
MIN_SQUEEZE_BARS = 6
VOL_MULT = 1.5
WINDOW_START_CT = dtime(9, 0)
WINDOW_END_CT = dtime(13, 55)  # last bar START that can signal
CHECKPOINTS_MIN = (15, 30, 60)
EOD_CHECK_CT = dtime(14, 55)
LOOKBACK_DAYS = 4  # calendar days of 5m bars for warm-up (covers a weekend)
POLL_SEC = 30
QUIET_POLL_SEC = 300


# ================================================================= helpers


def _ct(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CT)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number):
        return None
    return number


def _hm(moment: datetime) -> str:
    local = _ct(moment)
    return local.strftime("%I:%M %p").lstrip("0") + " CT"


def env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def telegram_enabled() -> bool:
    return env_flag("SQUEEZE_FIRE_TELEGRAM", True)


def resolve_symbols(config: Mapping[str, Any] | None = None) -> list[str]:
    actions = dict((config or {}).get("actions") or {})
    raw = actions.get("squeeze_fire_symbols") or list(DEFAULT_SYMBOLS)
    if isinstance(raw, str):
        raw = [s for s in raw.replace(",", " ").split() if s]
    out: list[str] = []
    for sym in raw:
        key = str(sym).strip().upper()
        if key and key not in EXCLUDED and key not in out:
            out.append(key)
    return out


# ================================================================= indicators


def _ema(values: list[float], length: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (length + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + alpha * (v - out[-1]))
    return out


def _linreg_last(y: list[float]) -> float:
    """Least-squares line through y (x = 0..n-1), value at the last x."""
    n = len(y)
    if n < 3:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(y) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (v - my) for i, v in enumerate(y))
    slope = sxy / sxx if sxx else 0.0
    return my + slope * ((n - 1) - mx)


def compute_indicators(bars: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per-bar squeeze state. ``bars`` are oldest first with t/o/h/l/c/v.

    Matches the backtest: population std, ATR = simple mean of true range
    over 20 bars, momentum = linear regression of close minus the average of
    the 20-bar Donchian mid and the 20-bar SMA, VWAP reset each CT day.
    """
    closes = [float(b["c"]) for b in bars]
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    vols = [float(b.get("v") or 0.0) for b in bars]
    e9 = _ema(closes, 9)
    e21 = _ema(closes, 21)
    out: list[dict[str, Any]] = []
    vw_day = None
    cum_v = cum_pv = 0.0
    trs: list[float] = []
    for i, bar in enumerate(bars):
        prev_c = closes[i - 1] if i else closes[0]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev_c), abs(lows[i] - prev_c)))
        j = max(0, i - BB_LEN + 1)
        win = closes[j : i + 1]
        sma = sum(win) / len(win)
        sd = math.sqrt(sum((x - sma) ** 2 for x in win) / len(win))
        atr = sum(trs[j : i + 1]) / len(win)
        upper_bb = sma + BB_MULT * sd
        lower_bb = sma - BB_MULT * sd
        squeeze_on = upper_bb < sma + KC_MULT * atr and lower_bb > sma - KC_MULT * atr
        mid = (max(highs[j : i + 1]) + min(lows[j : i + 1])) / 2.0
        mom = _linreg_last([x - (mid + sma) / 2.0 for x in win])
        day = _ct(bar["t"]).date()
        if day != vw_day:
            vw_day, cum_v, cum_pv = day, 0.0, 0.0
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_v += vols[i]
        cum_pv += tp * vols[i]
        vwap = cum_pv / cum_v if cum_v > 0 else closes[i]
        avg_vol = sum(vols[max(0, i - 20) : i]) / len(vols[max(0, i - 20) : i]) if i else 0.0
        out.append(
            {
                "t": bar["t"],
                "c": closes[i],
                "v": vols[i],
                "upper_bb": upper_bb,
                "squeeze_on": squeeze_on,
                "mom": mom,
                "e9": e9[i],
                "e21": e21[i],
                "vwap": vwap,
                "avg_vol20": avg_vol,
            }
        )
    return out


def closed_bars(bars: Iterable[Mapping[str, Any]], now: datetime, minutes: int = BAR_MIN) -> list[dict[str, Any]]:
    """Only bars whose 5 minutes are over. A forming bar never signals."""
    cutoff = _ct(now)
    return [dict(b) for b in bars if _ct(b["t"]) + timedelta(minutes=minutes) <= cutoff]


def evaluate_bar(ind: list[Mapping[str, Any]], i: int) -> tuple[bool, list[str]]:
    """Return (fired, failed_checks) for bar ``i`` of ``compute_indicators``."""
    fails: list[str] = []
    if i < BB_LEN + 1:
        return False, ["not enough bars"]
    row, prev = ind[i], ind[i - 1]
    run = 0
    k = i - 1
    while k >= 0 and ind[k]["squeeze_on"]:
        run += 1
        k -= 1
    clock = _ct(row["t"]).time()
    if not (WINDOW_START_CT <= clock <= WINDOW_END_CT):
        fails.append("outside 9:00–1:55 CT")
    if run < MIN_SQUEEZE_BARS:
        fails.append(f"squeeze on {run} bars < {MIN_SQUEEZE_BARS}")
    if row["squeeze_on"]:
        fails.append("squeeze still on")
    if not (row["mom"] > 0 and row["mom"] > prev["mom"]):
        fails.append("momentum not positive and rising")
    if not row["c"] > row["vwap"]:
        fails.append("close not above VWAP")
    if not (row["c"] > row["e9"] > row["e21"]):
        fails.append("close/EMA9/EMA21 not stacked")
    if not row["c"] > row["upper_bb"]:
        fails.append("close not above upper band")
    if not (row["avg_vol20"] > 0 and row["v"] > VOL_MULT * row["avg_vol20"]):
        fails.append(f"volume < {VOL_MULT}x average")
    return (not fails), fails


def detect_fire(bars: list[Mapping[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Signal on the newest CLOSED bar, or None."""
    done = closed_bars(bars, now)
    if len(done) < BB_LEN + 2:
        return None
    ind = compute_indicators(done)
    i = len(ind) - 1
    fired, _ = evaluate_bar(ind, i)
    if not fired:
        return None
    row = ind[i]
    start = _ct(row["t"])
    return {
        "bar_start_ct": start.isoformat(),
        "bar_close_ct": (start + timedelta(minutes=BAR_MIN)).isoformat(),
        "close": round(row["c"], 4),
        "vwap": round(row["vwap"], 4),
        "ema9": round(row["e9"], 4),
        "ema21": round(row["e21"], 4),
        "upper_bb": round(row["upper_bb"], 4),
        "vol_ratio": round(row["v"] / row["avg_vol20"], 2) if row["avg_vol20"] else None,
        "momentum": round(row["mom"], 4),
    }


# ================================================================= data (read only)


def _bars_from_tradier(symbol: str, now: datetime) -> list[dict[str, Any]] | None:
    from odte_scanner.data.tradier import access_token_from_env, fetch_timesales

    if not access_token_from_env():
        return None
    et = _ct(now).astimezone(ET)
    start = (et - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d 09:30")
    end = et.strftime("%Y-%m-%d %H:%M")
    res = fetch_timesales(symbol, interval="5min", start=start, end=end, session_filter="open", timeout=10.0)
    if not res.get("ok"):
        return None
    out: list[dict[str, Any]] = []
    for row in res.get("bars") or []:
        raw = row.get("time") or row.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        o, h, l, c = (_num(row.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        out.append({"t": ts, "o": o, "h": h, "l": l, "c": c, "v": _num(row.get("volume")) or 0.0})
    return out or None


def _bars_from_yahoo(symbol: str, now: datetime) -> list[dict[str, Any]] | None:
    import requests

    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "5m", "range": "5d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10.0,
    )
    r.raise_for_status()
    res = ((r.json() or {}).get("chart") or {}).get("result") or []
    if not res:
        return None
    blob = res[0]
    quote = ((blob.get("indicators") or {}).get("quote") or [{}])[0]
    out: list[dict[str, Any]] = []
    for i, ts in enumerate(blob.get("timestamp") or []):
        vals = [_num((quote.get(k) or [None] * (i + 1))[i]) for k in ("open", "high", "low", "close")]
        if None in vals:
            continue
        t = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(CT)
        if not (dtime(8, 30) <= t.time() < dtime(15, 0)):
            continue
        vol = _num((quote.get("volume") or [None] * (i + 1))[i]) or 0.0
        out.append({"t": t, "o": vals[0], "h": vals[1], "l": vals[2], "c": vals[3], "v": vol})
    return out or None


def fetch_bars(symbol: str, now: datetime) -> list[dict[str, Any]] | None:
    try:
        bars = _bars_from_tradier(symbol, now)
        if bars:
            return bars
    except Exception as exc:  # noqa: BLE001
        logger.info("squeeze fire tradier bars %s: %s", symbol, exc)
    try:
        return _bars_from_yahoo(symbol, now)
    except Exception as exc:  # noqa: BLE001
        logger.info("squeeze fire yahoo bars %s: %s", symbol, exc)
        return None


def pick_call(symbol: str, spot: float, today: date) -> dict[str, Any] | None:
    """Nearest expiry on or after today; lowest strike at or above spot with a live market."""
    from odte_scanner.data.tradier import access_token_from_env, fetch_expirations, fetch_option_chain

    if not access_token_from_env() or str(symbol).upper() in EXCLUDED:
        return None
    expiry = None
    for raw in sorted(fetch_expirations(symbol).get("dates") or []):
        try:
            if date.fromisoformat(str(raw)[:10]) >= today:
                expiry = str(raw)[:10]
                break
        except ValueError:
            continue
    if not expiry:
        return None
    chain = fetch_option_chain(symbol, expiry).get("options") or []
    best = None
    for opt in chain:
        if str(opt.get("option_type") or "").lower() != "call":
            continue
        strike = _num(opt.get("strike"))
        bid = _num(opt.get("bid")) or 0.0
        ask = _num(opt.get("ask")) or 0.0
        if strike is None or strike < spot or ask <= 0:
            continue
        if best is None or strike < best[0]:
            best = (strike, opt, bid, ask)
    if best is None:
        return None
    strike, opt, bid, ask = best
    return {
        "symbol": str(symbol).upper(),
        "contract": str(opt.get("symbol") or "").upper(),
        "expiry": expiry,
        "strike": strike,
        "right": "C",
        "dte": (date.fromisoformat(expiry) - today).days,
        "bid": bid if bid > 0 else None,
        "ask": ask,
    }


def quote_contract(row: Mapping[str, Any]) -> dict[str, Any] | None:
    from odte_scanner.options.contract_quote import fetch_contract_quote

    return fetch_contract_quote(dict(row))


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


def append_study(row: Mapping[str, Any], path: Path | None = None) -> None:
    from odte_scanner.json_util import dumps_strict

    dest = path or STUDY_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(dumps_strict(dict(row), default=str) + "\n")


def read_study(path: Path | None = None, limit: int = 2000) -> list[dict[str, Any]]:
    dest = path or STUDY_STORE
    if not dest.exists():
        return []
    rows: list[dict[str, Any]] = []
    with dest.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows[-limit:]


def _fresh_state(day: str) -> dict[str, Any]:
    return {"day": day, "signals": [], "last_bar": {}, "errors": 0}


def _roll_day(state: dict[str, Any], day: str, study_path: Path | None) -> dict[str, Any]:
    """New CT day: archive yesterday's rows (whatever checkpoints they got)."""
    if state.get("day") == day:
        return state
    for row in state.get("signals") or []:
        if isinstance(row, dict) and not row.get("archived"):
            row["archived"] = True
            row["status"] = "closed" if row.get("status") != "no_contract" else row["status"]
            append_study(row, study_path)
    return _fresh_state(day)


# ================================================================= messages


def _px(value: Any) -> str:
    n = _num(value)
    return f"${n:.2f}" if n is not None and n > 0 else "n/a"


def format_alert(symbol: str, sig: Mapping[str, Any], pick: Mapping[str, Any] | None) -> str:
    start = _ct(datetime.fromisoformat(str(sig["bar_start_ct"])))
    close = start + timedelta(minutes=BAR_MIN)
    lines = [
        f"🗜️ SQUEEZE FIRED · {symbol} · ALERT ONLY (no order placed)",
        (
            f"5m bar {start.strftime('%I:%M').lstrip('0')}–{close.strftime('%I:%M %p').lstrip('0')} CT closed "
            f"{_px(sig.get('close'))} · above VWAP {_px(sig.get('vwap'))}, 9/21 EMA and upper band · "
            f"volume {sig.get('vol_ratio')}x avg"
        ),
    ]
    if pick:
        exp = str(pick.get("expiry") or "")
        exp_txt = f"{exp[5:7]}/{exp[8:10]}" if len(exp) == 10 else exp
        strike = _num(pick.get("strike")) or 0.0
        strike_txt = f"{strike:g}"
        lines.append(
            f"Suggested near-the-money call: {symbol} {exp_txt} ${strike_txt}C · "
            f"Live Bid {_px(pick.get('bid'))} / Live Ask {_px(pick.get('ask'))}"
        )
    else:
        lines.append("Suggested call: no live option quote available")
    lines.append(
        "Research alert for a 30-day paper study. The backtest of this rule showed no edge. "
        "Not a trade signal. Options can go to zero."
    )
    return "\n".join(lines).replace("Sure Winner", "Gold Setup")


def _default_send(text: str) -> dict[str, Any]:
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text, timeout=10.0)


# ================================================================= cycle


def _checkpoint_due(row: Mapping[str, Any]) -> list[tuple[str, datetime]]:
    close = datetime.fromisoformat(str(row["bar_close_ct"]))
    due = [(f"+{m}m", close + timedelta(minutes=m)) for m in CHECKPOINTS_MIN]
    eod = datetime.combine(close.date(), EOD_CHECK_CT, tzinfo=CT)
    due.append(("2:55 PM", eod))
    return due


def run_checkpoints(
    state: dict[str, Any],
    moment: datetime,
    *,
    option_quote: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None = None,
) -> int:
    """Record bid/ask at each due checkpoint. Returns marks written."""
    quote = option_quote or quote_contract
    now = _ct(moment)
    wrote = 0
    for row in state.get("signals") or []:
        if not isinstance(row, dict) or not row.get("contract") or row.get("status") != "open":
            continue
        marks = row.setdefault("marks", {})
        for name, due in _checkpoint_due(row):
            if name in marks or now < due:
                continue
            # A late wake (deploy, outage) records nothing rather than a wrong-time mark.
            if now - due > timedelta(minutes=10):
                marks[name] = {"missed": True, "at_ct": now.isoformat()}
                continue
            try:
                q = quote(row) or {}
            except Exception as exc:  # noqa: BLE001
                logger.info("squeeze fire mark %s: %s", row.get("contract"), exc)
                q = {}
            marks[name] = {"bid": _num(q.get("bid")), "ask": _num(q.get("ask")), "at_ct": now.isoformat()}
            wrote += 1
        if all(name in marks for name, _ in _checkpoint_due(row)):
            row["status"] = "complete"
    return wrote


def run_cycle(
    moment: datetime | None = None,
    *,
    symbols: list[str] | None = None,
    bars_fn: Callable[[str, datetime], list[dict[str, Any]] | None] | None = None,
    pick_fn: Callable[[str, float, date], dict[str, Any] | None] | None = None,
    option_quote: Callable[[Mapping[str, Any]], dict[str, Any] | None] | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    store_path: Path | None = None,
    study_path: Path | None = None,
) -> dict[str, Any]:
    """One pass: new signals on just-closed bars, then due checkpoints. Never orders."""
    now = _ct(moment or datetime.now(timezone.utc))
    day = now.date().isoformat()
    state = _roll_day(read_state(store_path), day, study_path)
    syms = [s for s in (symbols or list(DEFAULT_SYMBOLS)) if s.upper() not in EXCLUDED]
    bars_fn = bars_fn or fetch_bars
    pick_fn = pick_fn or pick_call
    send = send or _default_send
    fired: list[str] = []

    if now.weekday() < 5 and WINDOW_START_CT <= now.time() <= dtime(14, 5):
        # Newest bar that could have closed by now.
        boundary = now.replace(second=0, microsecond=0)
        boundary = boundary.replace(minute=boundary.minute - boundary.minute % BAR_MIN)
        want_start = boundary - timedelta(minutes=BAR_MIN)
        done_today = {r.get("symbol") for r in state.get("signals") or []}
        last_bar = state.setdefault("last_bar", {})
        for sym in syms:
            if sym in done_today or last_bar.get(sym) == want_start.isoformat():
                continue
            try:
                bars = bars_fn(sym, now) or []
            except Exception as exc:  # noqa: BLE001
                logger.info("squeeze fire bars %s: %s", sym, exc)
                continue
            done = closed_bars(bars, now)
            if not done:
                continue
            newest = _ct(done[-1]["t"])
            # Provider still behind: try again next pass. Stale data never signals.
            if newest < want_start - timedelta(minutes=BAR_MIN):
                continue
            # One bar behind is evaluated now and again once the newest bar lands.
            last_bar[sym] = newest.isoformat()
            sig = detect_fire(done, now)
            if not sig:
                continue
            pick = None
            try:
                pick = pick_fn(sym, float(sig["close"]), now.date())
            except Exception as exc:  # noqa: BLE001
                logger.info("squeeze fire chain %s: %s", sym, exc)
            row = {
                "id": f"sqz-{sym}-{day}",
                "day": day,
                "symbol": sym,
                **sig,
                "alert_only": True,
                "orders": False,
                "detected_ct": now.isoformat(),
                "status": "open" if pick else "no_contract",
                "marks": {},
            }
            if pick:
                row.update(
                    contract=pick.get("contract"),
                    expiry=pick.get("expiry"),
                    strike=pick.get("strike"),
                    right="C",
                    dte=pick.get("dte"),
                    signal_bid=_num(pick.get("bid")),
                    signal_ask=_num(pick.get("ask")),
                )
            text = format_alert(sym, sig, pick)
            sent: dict[str, Any] = {"ok": False, "skipped": True}
            if telegram_enabled():
                try:
                    sent = send(text) or {}
                except Exception as exc:  # noqa: BLE001
                    sent = {"ok": False, "error": str(exc)[:200]}
            row["telegram"] = {"ok": bool(sent.get("ok")), "skipped": bool(sent.get("skipped"))}
            row["message"] = text
            state.setdefault("signals", []).append(row)
            fired.append(sym)
            logger.info("squeeze fire %s %s (alert only)", sym, row.get("contract"))

    marks = run_checkpoints(state, now, option_quote=option_quote)
    state["updated_at"] = now.isoformat()
    write_state(state, store_path)
    return {"fired": fired, "marks": marks, "day": day}


# ================================================================= portal block


def study_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Ask at signal vs bid at each checkpoint (what a buyer could have sold for)."""
    out: dict[str, Any] = {"n": 0}
    rows = [r for r in rows if _num(r.get("signal_ask"))]
    out["n"] = len(rows)
    for name in [f"+{m}m" for m in CHECKPOINTS_MIN] + ["2:55 PM"]:
        rets = []
        for r in rows:
            mark = (r.get("marks") or {}).get(name) or {}
            bid = _num(mark.get("bid"))
            if bid is None or mark.get("missed"):
                continue
            rets.append(bid / float(r["signal_ask"]) - 1.0)
        if rets:
            out[name] = {
                "n": len(rets),
                "avg_pct": round(100 * sum(rets) / len(rets), 1),
                "win_pct": round(100 * sum(1 for x in rets if x > 0) / len(rets), 1),
            }
    return out


def build_block(*, store_path: Path | None = None, study_path: Path | None = None) -> dict[str, Any]:
    state = read_state(store_path)
    history = read_study(study_path)
    today = [r for r in state.get("signals") or [] if isinstance(r, dict)]
    keys = ("symbol", "bar_close_ct", "close", "contract", "expiry", "strike", "signal_bid", "signal_ask",
            "marks", "status", "telegram")
    return {
        "ok": True,
        "label": LABEL,
        "alert_only": True,
        "orders": False,
        "day": state.get("day"),
        "today": [{k: r.get(k) for k in keys} for r in today],
        "study": study_stats(history + today),
        "study_days": len({r.get("day") for r in history + today if r.get("day")}),
        "telegram": telegram_enabled(),
        "updated_at": state.get("updated_at"),
        "note": NOTE,
    }


def public_block() -> dict[str, Any]:
    try:
        return build_block()
    except Exception as exc:  # noqa: BLE001
        logger.warning("squeeze fire block failed: %s", exc)
        return {"ok": False, "label": LABEL, "alert_only": True, "today": [], "error": str(exc)[:200], "note": NOTE}


# ================================================================= worker thread

_worker_started = False
_worker_lock = threading.Lock()


def _sleep_for(moment: datetime) -> float:
    local = _ct(moment)
    if local.weekday() < 5 and dtime(8, 55) <= local.time() <= dtime(15, 0):
        return float(POLL_SEC)
    return float(QUIET_POLL_SEC)


def _worker_loop(config_path: str | None) -> None:
    symbols = list(DEFAULT_SYMBOLS)
    try:
        from odte_scanner.config import load_config

        symbols = resolve_symbols(load_config(config_path))
    except Exception as exc:  # noqa: BLE001
        logger.info("squeeze fire config: %s", exc)
    logger.info("Squeeze Fire started (alert only) symbols=%s telegram=%s", ",".join(symbols), telegram_enabled())
    while True:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        local = _ct(now)
        if local.weekday() < 5 and dtime(8, 55) <= local.time() <= dtime(15, 5):
            try:
                run_cycle(now, symbols=symbols)
            except Exception as exc:  # noqa: BLE001
                logger.exception("squeeze fire cycle failed: %s", exc)
        time.sleep(max(5.0, _sleep_for(now) - (time.monotonic() - started)))


def squeeze_fire_enabled(config_path: str | None = None) -> bool:
    if not env_flag("SQUEEZE_FIRE_LOOP", True):
        return False
    try:
        from odte_scanner.config import load_config

        cfg = load_config(config_path)
        return bool((cfg.get("actions") or {}).get("squeeze_fire_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def start_squeeze_fire_worker(config_path: str | None = None) -> bool:
    """Start the loop once per process on its own daemon thread."""
    global _worker_started
    if (os.environ.get("SIGNAL_DESK_OFFLINE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if not squeeze_fire_enabled(config_path):
        logger.info("Squeeze Fire disabled")
        return False
    with _worker_lock:
        if _worker_started:
            return False
        _worker_started = True
    threading.Thread(target=_worker_loop, args=(config_path,), name="squeeze-fire-worker", daemon=True).start()
    return True
