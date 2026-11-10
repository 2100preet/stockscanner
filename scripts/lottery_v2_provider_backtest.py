"""Replay lottery v2 on the desk's live providers. Do not print credentials.

Equity bars: Tradier /markets/timesales (production, 5-minute). That is the
same intraday feed the desk uses for ORB and power hour.
Option bars: Polygon minute aggregates, one daily flat file per session.
Per-contract REST aggregates are capped near 5 calls/minute on this key, so
they cannot cover the strike grid. Tradier timesales rejects OCC symbols
(HTTP 400). Polygon historical option quotes return HTTP 403, so the 45%
spread filter cannot be applied and the entry is the trade close.

Writes outputs/lottery_v2_provider_backtest.json as it goes. Secrets are
scrubbed from errors before anything is stored or printed.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import signal
import sys
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
START = date.fromisoformat(os.environ.get("LOTTERY_V2_START") or "2026-10-01")
END = date.fromisoformat(os.environ.get("LOTTERY_V2_END") or "2026-10-14")
SYMBOLS = [
    "SPY", "QQQ", "IWM", "DIA", "META", "AAPL", "NVDA", "TSLA",
    "AMD", "AMZN", "MSFT", "GOOGL", "NFLX", "AVGO", "PFE", "JNJ",
]
POLY = "https://api.polygon.io"
FLAT_HOSTS = ("https://files.polygon.io", "https://files.massive.com")
socket_timeout = (5, 20)
STOP = False
# This key accepts about five aggregate calls a minute once it starts returning 429.
NEXT_OK = 0.0
INTERVAL = 1.0
PACE_LOCKED = False
POLY_CALLS = 0


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


def _pace() -> None:
    global NEXT_OK
    now = time.time()
    if now < NEXT_OK:
        time.sleep(NEXT_OK - now)
    NEXT_OK = time.time() + INTERVAL


def _note_status(code: int) -> None:
    global INTERVAL, PACE_LOCKED
    if code == 429 and not PACE_LOCKED:
        PACE_LOCKED = True
        INTERVAL = 12.5
        print("pace_lock", INTERVAL, "calls", POLY_CALLS, flush=True)
    elif code == 200 and not PACE_LOCKED:
        INTERVAL = max(0.35, INTERVAL * 0.92)


def _poly_get(path: str, params: dict) -> tuple[dict | None, str | None]:
    global POLY_CALLS
    key = api_key_from_env()
    if not key:
        return None, "POLYGON_API_KEY unset"
    query = dict(params)
    query["apiKey"] = key
    last_err = "polygon empty"
    for attempt in range(2):
        _pace()
        POLY_CALLS += 1
        try:
            response = requests.get(f"{POLY}{path}", params=query, timeout=socket_timeout)
        except Exception as exc:  # noqa: BLE001
            last_err = scrub(exc)
            continue
        _note_status(response.status_code)
        if response.status_code == 429:
            last_err = "polygon 429"
            continue
        if response.status_code in {401, 403}:
            return None, f"polygon {response.status_code}"
        if response.status_code >= 400:
            return None, f"polygon {response.status_code}"
        try:
            return response.json(), None
        except Exception as exc:  # noqa: BLE001
            return None, scrub(exc)
    return None, last_err


def polygon_aggs(symbol: str, start: str, end: str, minutes: int) -> tuple[list[dict], str | None]:
    if symbol == "VIX":
        ticker = "I:VIX"
    elif symbol.startswith("O:") or symbol in SYMBOLS:
        ticker = symbol
    else:
        ticker = f"O:{symbol}"
    payload, err = _poly_get(
        f"/v2/aggs/ticker/{ticker}/range/{minutes}/minute/{start}/{end}",
        {"adjusted": "true", "sort": "asc", "limit": 50000},
    )
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
    """Same 8-strike ladder the Yahoo replay used."""
    step = 1.0 if symbol in {"SPY", "QQQ", "IWM", "DIA"} or spot < 200 else 5.0
    base = round(spot / step) * step
    if side == "put":
        return [round(base - i * step, 2) for i in range(0, 8)]
    return [round(base + i * step, 2) for i in range(0, 8)]


def first_print(bars: list[dict], signal: datetime) -> dict | None:
    for bar in bars:
        ts = datetime.fromisoformat(bar["ts"])
        if ts >= signal and ts.date() == signal.date():
            return bar
    return None


def load_equity(symbol: str, source: dict[str, str]) -> list[dict]:
    start = f"{START.isoformat()} 09:30"
    end = f"{END.isoformat()} 16:00"
    bars, _err = tradier_bars(symbol, start, end, "5min")
    covered = {b["ts"][:10] for b in bars}
    if len(covered) >= 8:
        source[symbol] = "tradier"
        return bars
    merged = list(bars)
    have = set(covered)
    for day in sessions():
        if day.isoformat() in have:
            continue
        day_bars, _ = tradier_bars(symbol, f"{day.isoformat()} 09:30", f"{day.isoformat()} 16:00", "5min")
        if day_bars:
            merged.extend(day_bars)
            have.add(day.isoformat())
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


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _side_stats(rows: list[dict]) -> dict:
    usable = [r for r in rows if r["ladder"].get("ev_per_100") is not None]
    if not usable:
        return {"n": 0}
    evs = [r["ladder"]["ev_per_100"] for r in usable]
    return {
        "n": len(usable),
        "hit_5x": sum(1 for r in usable if r["ladder"]["hit_5x"]),
        "hit_10x": sum(1 for r in usable if r["ladder"]["hit_10x"]),
        "went_to_zero": sum(1 for r in usable if r["ladder"]["went_to_zero"]),
        "median_ev_per_100": round(_median(evs), 2),
        "mean_ev_per_100": round(sum(evs) / len(evs), 2),
    }


def summarize(rows: list[dict]) -> dict:
    usable = [r for r in rows if r["ladder"].get("ev_per_100") is not None]
    if not usable:
        return {"n": 0}
    evs = [r["ladder"]["ev_per_100"] for r in usable]
    mults = [r["ladder"]["blended_multiple"] for r in usable]
    return {
        "n": len(usable),
        "hit_5x": sum(1 for r in usable if r["ladder"]["hit_5x"]),
        "hit_10x": sum(1 for r in usable if r["ladder"]["hit_10x"]),
        "went_to_zero": sum(1 for r in usable if r["ladder"]["went_to_zero"]),
        "hit_5x_rate": round(sum(1 for r in usable if r["ladder"]["hit_5x"]) / len(usable), 3),
        "hit_10x_rate": round(sum(1 for r in usable if r["ladder"]["hit_10x"]) / len(usable), 3),
        "zero_rate": round(sum(1 for r in usable if r["ladder"]["went_to_zero"]) / len(usable), 3),
        "median_ev_per_100": round(_median(evs), 2),
        "mean_ev_per_100": round(sum(evs) / len(evs), 2),
        "median_blended_multiple": round(_median(mults), 4),
        "puts": _side_stats([r for r in usable if r["side"] == "put"]),
        "calls": _side_stats([r for r in usable if r["side"] == "call"]),
        "with_nbbo": sum(1 for r in usable if r.get("nbbo")),
    }


def cap_three_per_day(rows: list[dict]) -> list[dict]:
    """Live desk keeps 3 LOG tickets a day, confirms then cheaper ask."""
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["day"], []).append(row)
    kept: list[dict] = []
    for day in sorted(by_day):
        ranked = sorted(by_day[day], key=lambda r: (-int(r.get("confirms") or 0), float(r["entry"])))
        kept.extend(ranked[:3])
    return kept


def _write(payload: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(OUT)


def _flat_path(host: str, day: str) -> str:
    year, month, _rest = day.split("-")
    return f"{host}/us_options_opra/minute_aggs_v1/{year}/{month}/{day}.csv.gz"


def _flat_get(host: str, day: str, mode: str) -> requests.Response:
    key = api_key_from_env() or ""
    url = _flat_path(host, day)
    if mode == "bearer":
        return requests.get(
            url,
            headers={"Authorization": f"Bearer {key}"},
            stream=True,
            timeout=(20, 300),
        )
    return requests.get(url, params={"apiKey": key}, stream=True, timeout=(20, 300))


def _s3_client(endpoint: str):
    import boto3
    from botocore.config import Config

    key = api_key_from_env() or ""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _flat_object_key(day: str) -> str:
    year, month, _rest = day.split("-")
    return f"us_options_opra/minute_aggs_v1/{year}/{month}/{day}.csv.gz"


def open_s3(day: str) -> tuple[object | None, str, list[dict]]:
    """Head the daily minute-aggregate object. Notes contain status codes only."""
    notes: list[dict] = []
    if not api_key_from_env():
        return None, "", [{"error": "POLYGON_API_KEY unset"}]
    from botocore.exceptions import ClientError

    for endpoint in FLAT_HOSTS:
        try:
            client = _s3_client(endpoint)
            head = client.head_object(Bucket="flatfiles", Key=_flat_object_key(day))
        except ClientError as exc:
            meta = (exc.response or {}).get("ResponseMetadata") or {}
            err = (exc.response or {}).get("Error") or {}
            notes.append({
                "endpoint": endpoint,
                "auth": "s3v4",
                "status": meta.get("HTTPStatusCode"),
                "error": err.get("Code"),
            })
            continue
        except Exception as exc:  # noqa: BLE001
            notes.append({"endpoint": endpoint, "auth": "s3v4", "error": scrub(exc)})
            continue
        notes.append({
            "endpoint": endpoint,
            "auth": "s3v4",
            "status": 200,
            "bytes": head.get("ContentLength"),
        })
        return client, endpoint, notes
    return None, "", notes


def download_s3(client: object, day: str) -> Path:
    path = Path("/tmp") / f"lottery-flat-{day}.csv.gz"
    seen = {"n": 0}

    def hook(amount: int) -> None:
        seen["n"] += int(amount)
        step = 40 * 1024 * 1024
        if seen["n"] // step != (seen["n"] - int(amount)) // step:
            print("download", day, seen["n"], flush=True)

    client.download_file("flatfiles", _flat_object_key(day), str(path), Callback=hook)  # type: ignore[attr-defined]
    print("downloaded", day, path.stat().st_size, flush=True)
    return path


def open_flat(day: str) -> tuple[requests.Response | None, str, str, list[dict]]:
    """Return an open 200 response, the host, the auth mode, and status notes."""
    notes: list[dict] = []
    if not api_key_from_env():
        return None, "", "", [{"error": "POLYGON_API_KEY unset"}]
    for host in FLAT_HOSTS:
        for mode in ("query", "bearer"):
            try:
                response = _flat_get(host, day, mode)
            except Exception as exc:  # noqa: BLE001
                notes.append({"host": host, "auth": mode, "error": scrub(exc)})
                continue
            notes.append({
                "host": host,
                "auth": mode,
                "status": response.status_code,
                "bytes": response.headers.get("Content-Length"),
            })
            if response.status_code == 200:
                return response, host, mode, notes
            response.close()
    return None, "", "", notes


def _consume_flat(response: requests.Response, day: str, wanted: set[str], cache: dict[str, list[dict]]) -> int:
    path = Path("/tmp") / f"lottery-flat-{day}.csv.gz"
    written = 0
    try:
        with path.open("wb") as handle:
            for chunk in response.iter_content(8 * 1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
                if written // (40 * 1024 * 1024) != (written - len(chunk)) // (40 * 1024 * 1024):
                    print("download", day, written, flush=True)
    finally:
        response.close()
    print("downloaded", day, written, flush=True)
    return _parse_flat_path(path, day, wanted, cache)


def _parse_flat_path(path: Path, day: str, wanted: set[str], cache: dict[str, list[dict]]) -> int:
    kept = 0
    rows = 0
    with path.open("rb") as raw:
        magic = raw.read(2)
    opener = gzip.open if magic == b"\x1f\x8b" else open
    with opener(path, "rt", newline="") as handle:
        reader = csv.reader(handle)
        first = next(reader, None)
        if not first:
            path.unlink(missing_ok=True)
            return 0
        if first[0].lower() == "ticker":
            header = [name.strip().lower() for name in first]
            data_rows = reader
        else:
            header = ["ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"]

            def data_rows():  # type: ignore[no-redef]
                yield first
                yield from reader

            data_rows = data_rows()
        idx = {name: pos for pos, name in enumerate(header)}

        def cell(row: list[str], name: str, default: int) -> str:
            pos = idx.get(name, default)
            if pos >= len(row):
                return ""
            return row[pos]

        for row in data_rows:
            rows += 1
            if rows % 2_000_000 == 0:
                print("parse", day, "rows", rows, "kept_contracts", len(cache), flush=True)
            ticker = cell(row, "ticker", 0)
            if ticker not in wanted:
                continue
            stamp = cell(row, "window_start", 6)
            try:
                stamp_val: object = int(stamp)
            except ValueError:
                stamp_val = stamp
            raw = {
                "t": stamp_val,
                "o": cell(row, "open", 2),
                "h": cell(row, "high", 4),
                "l": cell(row, "low", 5),
                "c": cell(row, "close", 3),
                "v": cell(row, "volume", 1),
            }
            bar = _bar(raw)
            if bar is None:
                continue
            occ_symbol = ticker[2:] if ticker.startswith("O:") else ticker
            bucket = cache.setdefault(f"{occ_symbol}|{day}", [])
            if not bucket:
                kept += 1
            bucket.append(bar)
    path.unlink(missing_ok=True)
    for key, bars in cache.items():
        if key.endswith(f"|{day}"):
            bars.sort(key=lambda item: item["ts"])
    print("parsed", day, "rows", rows, "contracts", kept, flush=True)
    return kept


def _print_px(bars: list[dict], signal_ts: datetime) -> float | None:
    bar = first_print(bars, signal_ts)
    if bar is None:
        return None
    try:
        return float(bar["close"])
    except (TypeError, ValueError):
        return None


def fill_signal(sig: dict, cache: dict[str, list[dict]], errors: dict[str, int]) -> None:
    """Fetch the strikes whose prints can fall in $0.05–$0.40.

    Premium falls as the strike moves further out of the money, so the walk
    stops once it has the in-band neighbors. At most seven aggregates per expiry.
    """
    brk = sig["break"]
    signal_ts = datetime.fromisoformat(brk["ts"])
    for expiry, _dte in ((sig["day"], 0), (next_bday(sig["day"]), 1)):
        strikes = grid(sig["symbol"], float(brk["last"]), brk["side"])
        fetched: dict[int, float | None] = {}

        def grab(index: int) -> float | None:
            if index < 0 or index >= len(strikes):
                return None
            if index in fetched:
                return fetched[index]
            if len(fetched) >= 7:
                return None
            symbol = occ(sig["symbol"], expiry, brk["right"], strikes[index])
            key = f"{symbol}|{sig['day']}"
            if key not in cache:
                bars, err = polygon_aggs(symbol, sig["day"], sig["day"], 1)
                cache[key] = bars
                if err:
                    errors[err] = int(errors.get(err) or 0) + 1
            fetched[index] = _print_px(cache.get(key) or [], signal_ts)
            return fetched[index]

        idx = min(2, len(strikes) - 1)
        for _step in range(8):
            px = grab(idx)
            if len(fetched) >= 7:
                break
            if px is None:
                if idx + 1 < len(strikes) and (idx + 1) not in fetched:
                    idx += 1
                    continue
                if idx - 1 >= 0 and (idx - 1) not in fetched:
                    idx -= 1
                    continue
                break
            if px > 0.40:
                if idx + 1 >= len(strikes):
                    break
                idx += 1
                continue
            if px < 0.05:
                if idx - 1 < 0:
                    break
                idx -= 1
                continue
            grab(idx - 1)
            grab(idx + 1)
            for neighbor in (idx - 1, idx + 1):
                other = fetched.get(neighbor)
                if other is not None and 0.05 <= other <= 0.40:
                    grab(neighbor + (1 if neighbor > idx else -1))
            break


def score_signal(sig: dict, cache: dict[str, list[dict]], vix_bars: list[dict]) -> tuple[dict | None, str | None]:
    brk = sig["break"]
    signal_ts = datetime.fromisoformat(brk["ts"])
    vix = None
    for bar in vix_bars:
        if datetime.fromisoformat(bar["ts"]) <= signal_ts:
            vix = bar["close"]
    chosen = None
    saw_bar = False
    for expiry, dte in ((sig["day"], 0), (next_bday(sig["day"]), 1)):
        for strike in grid(sig["symbol"], float(brk["last"]), brk["side"]):
            symbol = occ(sig["symbol"], expiry, brk["right"], strike)
            bars = cache.get(f"{symbol}|{sig['day']}") or []
            if bars:
                saw_bar = True
            entry_bar = first_print(bars, signal_ts)
            if entry_bar is None:
                continue
            px = float(entry_bar["close"])
            if not (0.05 <= px <= 0.40):
                continue
            entry_ts = datetime.fromisoformat(entry_bar["ts"])
            vol = sum(
                int(b.get("volume") or 0)
                for b in bars
                if b["ts"][:10] == sig["day"] and datetime.fromisoformat(b["ts"]) <= entry_ts
            )
            row = {
                "occ": symbol,
                "strike": strike,
                "expiry": expiry,
                "dte": dte,
                "ask": px,
                "volume": vol,
                "entry_bar": entry_bar,
                "bars": bars,
                "dist": abs(px - 0.20),
                "liquid": vol >= 50,
            }
            if chosen is None:
                chosen = row
            elif row["liquid"] and not chosen["liquid"]:
                chosen = row
            elif row["liquid"] == chosen["liquid"] and row["dist"] < chosen["dist"]:
                chosen = row
    if chosen is None:
        return None, "no_option_bars" if not saw_bar else "no_contract_in_band"
    decision = decide_lottery_v2(
        {
            "symbol": sig["symbol"],
            "right": brk["right"],
            "contract": chosen["occ"],
            "strike": chosen["strike"],
            "expiry": chosen["expiry"],
            "dte": chosen["dte"],
            "ask": chosen["ask"],
            "bid": None,
            "volume": chosen["volume"],
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
    if decision.action != "LOG":
        reason = decision.detail or "gate"
        if "spread" in reason:
            return None, "spread"
        if "thin" in reason:
            return None, "thin"
        return None, "gate"
    later = [
        b for b in chosen["bars"]
        if datetime.fromisoformat(b["ts"]) > datetime.fromisoformat(chosen["entry_bar"]["ts"])
        and b["ts"][:10] == sig["day"]
    ]
    ladder = replay_ladder(chosen["ask"], later)
    return {
        "symbol": sig["symbol"],
        "day": sig["day"],
        "side": brk["side"],
        "occ": chosen["occ"],
        "dte": chosen["dte"],
        "entry": round(chosen["ask"], 4),
        "bid": None,
        "nbbo": False,
        "entry_kind": "trade_close",
        "signal": brk["ts"],
        "vix": vix,
        "volume": chosen["volume"],
        "confirms": decision.confirms,
        "tags": decision.confirm_tags,
        "ladder": ladder,
    }, None


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
    _opt, opt_err = tradier_bars("SPY261014P00770000", "2026-10-14 09:30", "2026-10-14 16:00", "1min")
    probe["tradier_option_1m"] = {"n": 0, "error": opt_err, "sample_keys_ok": False}
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
    if len(pagg) < 20:
        _write({
            "ok": False,
            "complete": False,
            "probe": probe,
            "reason": "Polygon option aggregates did not return a usable minute tape",
        })
        return

    payload = _base_payload(probe, {}, [], [], {}, complete=False)
    _write(payload)

    equity_source: dict[str, str] = {}
    equity = {sym: load_equity(sym, equity_source) for sym in SYMBOLS}
    vix_bars, vix_err = tradier_bars("VIX", f"{START.isoformat()} 09:30", f"{END.isoformat()} 16:00", "5min")
    vix_source = "tradier"
    if len(vix_bars) < 20:
        vix_bars, vix_err = polygon_aggs("VIX", START.isoformat(), END.isoformat(), 5)
        vix_source = "polygon" if len(vix_bars) >= 20 else "none"

    signals = []
    for sym, bars in equity.items():
        for _day, day_bars in by_day(bars).items():
            brk = scan_session_break(day_bars)
            if brk and START.isoformat() <= brk["ts"][:10] <= END.isoformat():
                signals.append({"symbol": sym, "day": brk["ts"][:10], "break": brk})
    print(
        "breaks", len(signals),
        "bar_source", "polygon",
        "nbbo", False,
        "vix", vix_source, len(vix_bars),
        flush=True,
    )

    probe["option_source"] = "polygon_aggs"
    probe["flatfile"] = "s3_403_http_404"
    print("breaks_in_window", len(signals), "start", START.isoformat(), "end", END.isoformat(), flush=True)
    cache: dict[str, list[dict]] = {}
    errors: dict[str, int] = {}
    started = time.time()
    by_session: dict[str, list[dict]] = {}
    for sig in signals:
        by_session.setdefault(sig["day"], []).append(sig)
    done_days: list[str] = []
    for day in sorted(by_session):
        if STOP:
            break
        for sig in by_session[day]:
            if STOP:
                break
            fill_signal(sig, cache, errors)
        if STOP:
            break
        done_days.append(day)
        errors["polygon_calls"] = POLY_CALLS
        _checkpoint(
            probe, equity_source, equity, signals, cache, vix_bars, errors, vix_source, started, False,
        )
        print("day_done", day, "calls", POLY_CALLS, "interval", round(INTERVAL, 2), flush=True)
    every_day = (not STOP) and done_days == sorted(by_session)
    errors["polygon_calls"] = POLY_CALLS
    payload = _checkpoint(
        probe, equity_source, equity, signals, cache, vix_bars, errors, vix_source, started, every_day,
    )
    if not every_day:
        payload["ok"] = False
        payload["complete"] = False
        payload["reason"] = "stopped before every session was fetched"
        _write(payload)
    elif int(errors.get("polygon 429") or 0) > 40:
        payload["ok"] = False
        payload["reason"] = "rate limit dropped option bars"
        _write(payload)
    print("SUMMARY", json.dumps(payload["summary"]), flush=True)
    print("CAPPED", json.dumps(payload["summary_max_3_per_day"]), flush=True)


def _base_payload(
    probe: dict,
    equity_source: dict,
    equity: dict,
    signals: list,
    skipped: dict,
    *,
    complete: bool,
) -> dict:
    equity_days = sorted({b["ts"][:10] for bars in equity.values() for b in bars}) if equity else []
    return {
        "ok": True,
        "complete": complete,
        "window": {"start": START.isoformat(), "end": END.isoformat(), "sessions": [d.isoformat() for d in sessions()]},
        "providers": {
            "live_priority": ["tradier", "polygon", "yahoo", "cache"],
            "equity_bars": equity_source or "tradier",
            "option_bars": probe.get("option_source") or "polygon_aggs",
            "nbbo": "unavailable" if not probe.get("polygon_nbbo", {}).get("ok") else "polygon",
            "spread_filter": "not_applied_nbbo_unavailable",
            "entry": "trade_close",
        },
        "probe": probe,
        "equity_days": equity_days,
        "breaks": len(signals),
        "skipped": skipped,
        "summary": {"n": 0},
        "tickets": [],
    }


def _checkpoint(
    probe: dict,
    equity_source: dict,
    equity: dict,
    signals: list,
    cache: dict,
    vix_bars: list,
    errors: dict,
    vix_source: str,
    started: float,
    complete: bool,
) -> dict:
    tickets = []
    skipped: dict[str, int] = {}
    sessions_with_bars = 0
    for sig in signals:
        if any(cache.get(f"{occ(sig['symbol'], expiry, sig['break']['right'], strike)}|{sig['day']}")
               for expiry, _dte in ((sig["day"], 0), (next_bday(sig["day"]), 1))
               for strike in grid(sig["symbol"], float(sig["break"]["last"]), sig["break"]["side"])):
            sessions_with_bars += 1
        ticket, reason = score_signal(sig, cache, vix_bars)
        if ticket is None:
            skipped[reason or "skip"] = skipped.get(reason or "skip", 0) + 1
            continue
        tickets.append(ticket)
    capped = cap_three_per_day(tickets)
    payload = _base_payload(probe, equity_source, equity, signals, skipped, complete=complete)
    payload["providers"]["vix"] = vix_source
    payload["providers"]["equity_by_symbol"] = equity_source
    payload["coverage"] = {
        "contracts_requested": len(cache),
        "contracts_with_bars": sum(1 for bars in cache.values() if bars),
        "sessions_with_any_option_bar": sessions_with_bars,
        "fetch_errors": errors,
        "elapsed_sec": round(time.time() - started, 1),
    }
    payload["summary"] = summarize(tickets)
    payload["summary_max_3_per_day"] = summarize(capped)
    payload["tickets"] = tickets
    if complete and payload["coverage"]["contracts_with_bars"] == 0:
        payload["ok"] = False
        payload["reason"] = "Polygon option aggregates returned no bars"
    _write(payload)
    print(
        "checkpoint",
        "complete" if complete else "partial",
        "tickets", len(tickets),
        "sessions_with_bars", sessions_with_bars,
        flush=True,
    )
    return payload


def _on_term(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True
    print("sigterm", flush=True)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_term)
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _write({"ok": False, "complete": False, "error": scrub(exc), "trace": scrub(traceback.format_exc())})
        raise
