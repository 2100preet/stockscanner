"""Replay lottery v2 on the desk's live providers. Do not print credentials.

Equity and option bars: Tradier /markets/timesales (the same intraday feed the
desk uses for ORB and power hour), then Polygon aggregates if Tradier is empty.
Entry bid/ask: Polygon historical option quotes when the plan returns them.
Tradier quotes are live-only, so they are not a historical spread source.

Writes outputs/lottery_v2_provider_backtest.json. Secrets are scrubbed from
errors before anything is stored.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from odte_scanner.data.polygon import api_key_from_env
from odte_scanner.data.tradier import access_token_from_env, fetch_timesales
from odte_scanner.signals.lottery_v2 import (
    decide_lottery_v2,
    replay_ladder,
    scan_session_break,
)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "lottery_v2_provider_backtest.json"
START = date(2026, 10, 1)
END = date(2026, 10, 14)
SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "META", "AAPL", "NVDA", "TSLA",
    "AMD", "AMZN", "MSFT", "GOOGL", "NFLX", "AVGO", "PFE", "JNJ",
]
POLY = "https://api.polygon.io"


def scrub(text: str) -> str:
    out = str(text or "")
    for name in ("POLYGON_API_KEY", "MASSIVE_API_KEY", "TRADIER_ACCESS_TOKEN"):
        secret = (os.environ.get(name) or "").strip()
        if secret:
            out = out.replace(secret, "REDACTED")
    return out[:500]


def sessions() -> list[date]:
    day = START
    out: list[date] = []
    while day <= END:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _as_et(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # Tradier timestamp is seconds. Polygon agg "t" is milliseconds.
        value = float(raw)
        if value > 1e14:
            value = value / 1e9
        elif value > 1e11:
            value = value / 1e3
        return datetime.fromtimestamp(value, ET)
    text = str(raw).replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


def _bar(raw: dict) -> dict | None:
    ts = _as_et(raw.get("time") or raw.get("timestamp") or raw.get("t"))
    close = raw.get("close", raw.get("c", raw.get("price")))
    high = raw.get("high", raw.get("h", close))
    low = raw.get("low", raw.get("l", close))
    if ts is None or close is None or high is None or low is None:
        return None
    try:
        return {
            "ts": ts.isoformat(),
            "open": float(raw.get("open") or raw.get("o") or close),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": int(float(raw.get("volume") or raw.get("v") or 0)),
        }
    except (TypeError, ValueError):
        return None


def tradier_bars(symbol: str, start: str, end: str, interval: str) -> tuple[list[dict], str | None]:
    if not access_token_from_env():
        return [], "TRADIER_ACCESS_TOKEN unset"
    try:
        res = fetch_timesales(symbol, interval=interval, start=start, end=end, session_filter="open")
    except Exception as exc:  # noqa: BLE001
        return [], scrub(exc)
    if not res.get("ok"):
        return [], scrub(res.get("error") or "tradier timesales failed")
    bars = [b for row in (res.get("bars") or []) if (b := _bar(row))]
    bars.sort(key=lambda b: b["ts"])
    return bars, None


def _poly_get(path: str, params: dict) -> tuple[dict | None, str | None]:
    key = api_key_from_env()
    if not key:
        return None, "POLYGON_API_KEY unset"
    try:
        query = dict(params)
        query["apiKey"] = key
        response = requests.get(f"{POLY}{path}", params=query, timeout=25)
    except Exception as exc:  # noqa: BLE001
        return None, scrub(exc)
    if response.status_code in {401, 403}:
        return None, f"polygon {response.status_code}"
    if response.status_code == 429:
        time.sleep(1.5)
        return None, "polygon 429"
    if response.status_code >= 400:
        return None, f"polygon {response.status_code}"
    try:
        return response.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, scrub(exc)


def polygon_aggs(symbol: str, start: str, end: str, minutes: int) -> tuple[list[dict], str | None]:
    ticker = symbol if symbol.startswith("O:") or symbol in SYMBOLS or symbol == "I:VIX" else f"O:{symbol}"
    if symbol == "VIX":
        ticker = "I:VIX"
    payload, err = _poly_get(
        f"/v2/aggs/ticker/{ticker}/range/{minutes}/minute/{start}/{end}",
        {"adjusted": "true", "sort": "asc", "limit": 50000},
    )
    time.sleep(0.12)
    if err or not payload:
        return [], err
    bars = [b for row in (payload.get("results") or []) if (b := _bar(row))]
    return bars, None


def polygon_nbbo(occ: str, when: datetime) -> tuple[dict | None, str | None]:
    ns = int(when.timestamp() * 1_000_000_000)
    payload, err = _poly_get(
        f"/v3/quotes/O:{occ}",
        {"timestamp.gte": ns, "order": "asc", "sort": "timestamp", "limit": 1},
    )
    if err or not payload:
        return None, err
    rows = payload.get("results") or []
    if not rows:
        return None, "no quote"
    row = rows[0]
    try:
        return {
            "bid": float(row.get("bid_price") or 0) or None,
            "ask": float(row.get("ask_price") or 0) or None,
        }, None
    except (TypeError, ValueError):
        return None, "bad quote"


def by_day(bars: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for bar in bars:
        out.setdefault(bar["ts"][:10], []).append(bar)
    return out


def next_bday(day: str) -> str:
    current = date.fromisoformat(day) + timedelta(days=1)
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current.isoformat()


def occ(symbol: str, day: str, right: str, strike: float) -> str:
    yymmdd = date.fromisoformat(day).strftime("%y%m%d")
    return f"{symbol}{yymmdd}{right}{int(round(strike * 1000)):08d}"


def grid(symbol: str, spot: float, side: str) -> list[float]:
    step = 1.0 if symbol in {"SPY", "QQQ", "IWM", "DIA"} or spot < 200 else 5.0
    base = round(spot / step) * step
    if side == "put":
        return [round(base - i * step, 2) for i in range(0, 6)]
    return [round(base + i * step, 2) for i in range(0, 6)]


def first_print(bars: list[dict], signal: datetime) -> dict | None:
    for bar in bars:
        ts = datetime.fromisoformat(bar["ts"])
        if ts >= signal and ts.date() == signal.date():
            return bar
    return None


def load_equity(symbol: str, source: dict[str, str]) -> list[dict]:
    start = f"{START.isoformat()} 09:30"
    end = f"{END.isoformat()} 16:00"
    bars, err = tradier_bars(symbol, start, end, "5min")
    covered = {b["ts"][:10] for b in bars}
    if len(covered) >= 8:
        source[symbol] = "tradier"
        return bars
    # A long timesales window often truncates. Pull each session.
    merged = list(bars)
    have = set(covered)
    for day in sessions():
        if day.isoformat() in have:
            continue
        day_bars, _ = tradier_bars(symbol, f"{day.isoformat()} 09:30", f"{day.isoformat()} 16:00", "5min")
        if day_bars:
            merged.extend(day_bars)
            have.add(day.isoformat())
        time.sleep(0.05)
    if len(have) >= 8:
        source[symbol] = "tradier"
        merged.sort(key=lambda b: b["ts"])
        return merged
    poly, _ = polygon_aggs(symbol, START.isoformat(), END.isoformat(), 5)
    if len({b["ts"][:10] for b in poly}) > len(have):
        source[symbol] = "polygon"
        return poly
    source[symbol] = "tradier" if merged else "none"
    merged.sort(key=lambda b: b["ts"])
    return merged


def option_bars(occ_symbol: str, day: str, prefer: str, cache: dict, *, polygon_ok: bool) -> list[dict]:
    key = f"{occ_symbol}|{day}"
    if key in cache:
        return cache[key]
    bars: list[dict] = []
    if prefer == "tradier":
        bars, _ = tradier_bars(occ_symbol, f"{day} 09:30", f"{day} 16:00", "1min")
        time.sleep(0.05)
    if not bars and polygon_ok:
        bars, _ = polygon_aggs(occ_symbol, day, day, 1)
    cache[key] = bars
    return bars


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    evs = sorted(r["ladder"]["ev_per_100"] for r in rows)
    mults = [r["ladder"]["blended_multiple"] for r in rows]

    def side(name: str) -> dict:
        sub = [r for r in rows if r["side"] == name]
        if not sub:
            return {"n": 0}
        return {
            "n": len(sub),
            "hit_5x": sum(1 for r in sub if r["ladder"]["hit_5x"]),
            "hit_10x": sum(1 for r in sub if r["ladder"]["hit_10x"]),
            "went_to_zero": sum(1 for r in sub if r["ladder"]["went_to_zero"]),
            "mean_ev_per_100": round(sum(r["ladder"]["ev_per_100"] for r in sub) / len(sub), 2),
        }

    return {
        "n": len(rows),
        "hit_5x": sum(1 for r in rows if r["ladder"]["hit_5x"]),
        "hit_10x": sum(1 for r in rows if r["ladder"]["hit_10x"]),
        "went_to_zero": sum(1 for r in rows if r["ladder"]["went_to_zero"]),
        "hit_5x_rate": round(sum(1 for r in rows if r["ladder"]["hit_5x"]) / len(rows), 3),
        "zero_rate": round(sum(1 for r in rows if r["ladder"]["went_to_zero"]) / len(rows), 3),
        "median_ev_per_100": evs[len(evs) // 2],
        "mean_ev_per_100": round(sum(evs) / len(evs), 2),
        "median_blended_multiple": round(sorted(mults)[len(mults) // 2], 4),
        "puts": side("put"),
        "calls": side("call"),
        "with_nbbo": sum(1 for r in rows if r.get("nbbo")),
    }


def main() -> None:
    print(
        "providers",
        "tradier", "set" if access_token_from_env() else "unset",
        "polygon", "set" if api_key_from_env() else "unset",
        flush=True,
    )
    probe: dict = {}
    spy, spy_err = tradier_bars("SPY", "2026-10-01 09:30", "2026-10-14 16:00", "5min")
    probe["tradier_spy_5m"] = {
        "n": len(spy),
        "days": sorted({b["ts"][:10] for b in spy}),
        "error": spy_err,
    }
    opt, opt_err = tradier_bars("SPY261014P00770000", "2026-10-14 09:30", "2026-10-14 16:00", "1min")
    probe["tradier_option_1m"] = {"n": len(opt), "error": opt_err, "sample_keys_ok": bool(opt)}
    pagg, pagg_err = polygon_aggs("SPY261014P00770000", "2026-10-14", "2026-10-14", 1)
    probe["polygon_option_aggs"] = {"n": len(pagg), "error": pagg_err}
    when = datetime(2026, 10, 14, 10, 0, tzinfo=ET)
    nbbo, nbbo_err = polygon_nbbo("SPY261014P00770000", when)
    probe["polygon_nbbo"] = {
        "ok": bool(nbbo and nbbo.get("ask")),
        "has_bid": bool(nbbo and nbbo.get("bid")),
        "error": nbbo_err,
    }
    print("probe", json.dumps(probe), flush=True)

    bar_source = "tradier" if len(opt) >= 20 else ("polygon" if len(pagg) >= 20 else "")
    use_nbbo = bool(nbbo and nbbo.get("ask"))
    if not bar_source:
        _write({"ok": False, "probe": probe, "reason": "no historical option bars from Tradier or Polygon"})
        return

    equity_source: dict[str, str] = {}
    equity = {sym: load_equity(sym, equity_source) for sym in SYMBOLS}
    vix_bars, _ = tradier_bars("VIX", f"{START.isoformat()} 09:30", f"{END.isoformat()} 16:00", "5min")
    if len(vix_bars) < 20:
        vix_bars, _ = polygon_aggs("VIX", START.isoformat(), END.isoformat(), 5)

    signals = []
    for sym, bars in equity.items():
        for _day, day_bars in by_day(bars).items():
            brk = scan_session_break(day_bars)
            if brk and START.isoformat() <= brk["ts"][:10] <= END.isoformat():
                signals.append({"symbol": sym, "day": brk["ts"][:10], "break": brk})
    print("breaks", len(signals), "bar_source", bar_source, "nbbo", use_nbbo, flush=True)

    cache: dict = {}
    tickets = []
    skipped: dict[str, int] = {}
    nbbo_errors = 0
    for index, sig in enumerate(signals):
        brk = sig["break"]
        signal_ts = datetime.fromisoformat(brk["ts"])
        vix = None
        for bar in vix_bars:
            if datetime.fromisoformat(bar["ts"]) <= signal_ts:
                vix = bar["close"]
        found = []
        for expiry, dte in ((sig["day"], 0), (next_bday(sig["day"]), 1)):
            for strike in grid(sig["symbol"], float(brk["last"]), brk["side"]):
                symbol = occ(sig["symbol"], expiry, brk["right"], strike)
                bars = option_bars(
                    symbol,
                    sig["day"],
                    bar_source,
                    cache,
                    polygon_ok=probe["polygon_option_aggs"]["n"] >= 20,
                )
                entry_bar = first_print(bars, signal_ts)
                if entry_bar is None:
                    continue
                px = float(entry_bar["close"])
                if px < 0.02 or px > 1.0:
                    continue
                entry_ts = datetime.fromisoformat(entry_bar["ts"])
                vol = sum(
                    int(b.get("volume") or 0)
                    for b in bars
                    if b["ts"][:10] == sig["day"] and datetime.fromisoformat(b["ts"]) <= entry_ts
                )
                quote = None
                if use_nbbo and 0.02 <= px <= 0.60:
                    quote, qerr = polygon_nbbo(symbol, signal_ts)
                    if qerr and qerr not in {"no quote"}:
                        nbbo_errors += 1
                        if nbbo_errors >= 8:
                            use_nbbo = False
                ask = px
                bid = None
                had_nbbo = False
                if quote and quote.get("ask"):
                    ask = float(quote["ask"])
                    bid = quote.get("bid")
                    had_nbbo = True
                if not (0.05 <= ask <= 0.40):
                    continue
                found.append({
                    "occ": symbol,
                    "strike": strike,
                    "expiry": expiry,
                    "dte": dte,
                    "ask": ask,
                    "bid": bid,
                    "volume": vol,
                    "nbbo": had_nbbo,
                    "entry_bar": entry_bar,
                    "bars": bars,
                    "dist": abs(ask - 0.20),
                })
        if not found:
            skipped["no_contract_in_band"] = skipped.get("no_contract_in_band", 0) + 1
            continue
        found.sort(key=lambda row: (not row["nbbo"], row["dist"]))
        chosen = None
        decision = None
        for row in found[:4]:
            decision = decide_lottery_v2(
                {
                    "symbol": sig["symbol"],
                    "right": brk["right"],
                    "contract": row["occ"],
                    "strike": row["strike"],
                    "expiry": row["expiry"],
                    "dte": row["dte"],
                    "ask": row["ask"],
                    "bid": row["bid"],
                    "volume": row["volume"],
                    "open_interest": 0,
                },
                tape={
                    "last": brk["last"],
                    "orb_high": brk["orb_high"],
                    "orb_low": brk["orb_low"],
                    "vwap": brk["vwap"],
                    "rel_volume": brk.get("rel_volume"),
                },
                now=signal_ts,
                vix=vix,
            )
            if decision.action == "LOG":
                chosen = row
                break
            reason = decision.detail
            key = "spread" if "spread" in reason else ("thin" if "thin" in reason else "gate")
            skipped[key] = skipped.get(key, 0) + 1
        if chosen is None or decision is None:
            continue
        later = [
            b for b in chosen["bars"]
            if datetime.fromisoformat(b["ts"]) > datetime.fromisoformat(chosen["entry_bar"]["ts"])
            and b["ts"][:10] == sig["day"]
        ]
        ladder = replay_ladder(chosen["ask"], later)
        tickets.append({
            "symbol": sig["symbol"],
            "day": sig["day"],
            "side": brk["side"],
            "occ": chosen["occ"],
            "dte": chosen["dte"],
            "entry": round(chosen["ask"], 4),
            "bid": chosen["bid"],
            "nbbo": chosen["nbbo"],
            "signal": brk["ts"],
            "vix": vix,
            "confirms": decision.confirms,
            "tags": decision.confirm_tags,
            "ladder": ladder,
        })
        if index % 10 == 0:
            print("progress", index, "tickets", len(tickets), flush=True)

    equity_days = sorted({b["ts"][:10] for bars in equity.values() for b in bars})
    out = {
        "ok": True,
        "providers": {
            "live_priority": ["tradier", "polygon", "yahoo", "cache"],
            "equity_bars": equity_source,
            "option_bars": bar_source,
            "nbbo": "polygon" if probe["polygon_nbbo"]["ok"] else "unavailable",
        },
        "probe": probe,
        "equity_days": equity_days,
        "breaks": len(signals),
        "skipped": skipped,
        "summary": summarize(tickets),
        "tickets": tickets,
    }
    _write(out)
    print("SUMMARY", json.dumps(out["summary"]), flush=True)


def _write(payload: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str))
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _write({"ok": False, "error": scrub(exc), "trace": scrub(traceback.format_exc())})
        raise
