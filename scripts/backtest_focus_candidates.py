"""Backtest candidate focus names against the current focus universe.

Two legs, both walk-forward with no look-ahead:

1. Daily quality signal (same method as ``backtest/win_rates.py``, which feeds
   the Gold Setup hist-win gate and BUY NOW): score bars[:i] with
   ``score_ticker`` and the config weights; when the horizon quality gate
   passes, buy an ATM call at the close and hold 1 session (0DTE/1DTE desks)
   or 5 sessions (weekly). Option P&L is a Black-Scholes reprice with IV =
   1.1 x 20-day realized vol, minus the bid/ask spread on both sides and
   $0.65/contract commission each side.

2. Power Hour replay on 5-minute bars (Yahoo keeps ~60 days): at each 5-minute
   decision from 15:00 to 15:45 ET call ``decide_power_hour`` on bars up to
   the decision clock. The first LONG/SHORT buys a 0DTE ATM call/put; exit at
   the signal stop or the 15:50 ET flatten. Same costs.

Run on the box, not on Fly:  python scripts/backtest_focus_candidates.py HUBS DUOL CRM
"""

from __future__ import annotations

import json
import math
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, time, timezone
from pathlib import Path
from statistics import mean

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from odte_scanner.algos.engine import QUALITY_GATES, score_ticker  # noqa: E402
from odte_scanner.config import load_config  # noqa: E402
from odte_scanner.data.fetcher import fetch_history  # noqa: E402
from odte_scanner.data.universe import FOCUS_DEFAULT  # noqa: E402

ET = "America/New_York"
RATE = 0.04
HALF_SPREAD_PCT = 0.03  # 3% of premium each side (liquid single-name options)
HALF_SPREAD_MIN = 0.025  # $0.025/share floor (a $0.05 wide market)
COMMISSION = 0.65 / 100.0  # per share, each side
HORIZONS = {"0dte": 1, "weekly": 5}
DTE_AT_ENTRY = {"0dte": 1, "weekly": 7}
SKIP = {"SPX", "XSP"}
DAILY_PERIOD = os.environ.get("FOCUS_BT_PERIOD", "5y")  # win_rates.py uses 2y; 5y gives rare signals a sample


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(spot: float, strike: float, years: float, sigma: float, right: str) -> float:
    if years <= 0 or sigma <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (RATE + 0.5 * sigma * sigma) * years) / (sigma * math.sqrt(years))
    d2 = d1 - sigma * math.sqrt(years)
    if right == "C":
        return spot * _ncdf(d1) - strike * math.exp(-RATE * years) * _ncdf(d2)
    return strike * math.exp(-RATE * years) * _ncdf(-d2) - spot * _ncdf(-d1)


def round_trip_pct(entry_mid: float, exit_mid: float) -> float:
    """Option P&L % of the premium paid after spread and commission."""
    if entry_mid <= 0.01:
        return float("nan")
    paid = entry_mid + max(HALF_SPREAD_MIN, entry_mid * HALF_SPREAD_PCT) + COMMISSION
    got = max(0.0, exit_mid - max(HALF_SPREAD_MIN, exit_mid * HALF_SPREAD_PCT)) - COMMISSION
    return (got - paid) / paid * 100.0


def realized_vol(close: pd.Series) -> float:
    rets = np.log(close / close.shift()).dropna().tail(20)
    if len(rets) < 10:
        return 0.4
    return max(0.12, float(rets.std() * math.sqrt(252)) * 1.1)


def strike_for(spot: float) -> float:
    step = 0.5 if spot < 25 else (1.0 if spot < 200 else 5.0)
    return round(spot / step) * step


def daily_leg(symbol: str, df: pd.DataFrame, spy: pd.DataFrame, vix: pd.DataFrame, weights: dict) -> list[dict]:
    rows: list[dict] = []
    if df is None or len(df) < 120:
        return rows
    for hz, fwd in HORIZONS.items():
        w = weights.get(hz) or weights.get("_legacy") or None
        gate = QUALITY_GATES.get(hz, QUALITY_GATES["0dte"])
        step = 1 if hz == "0dte" else 2
        for i in range(60, len(df) - fwd, step):
            window = df.iloc[: i + 1]
            try:
                ts = score_ticker(
                    symbol,
                    window,
                    spy_df=spy.loc[: window.index[-1]],
                    vix_df=vix.loc[: window.index[-1]],
                    weights=w,
                    horizon=hz,
                )
            except Exception:  # noqa: BLE001
                continue
            if not ts.quality or ts.ensemble_score < float(gate["min_score"]):
                continue
            spot = float(df["Close"].iloc[i])
            fut = float(df["Close"].iloc[i + fwd])
            if spot <= 0:
                continue
            sigma = realized_vol(window["Close"])
            k = strike_for(spot)
            dte = DTE_AT_ENTRY[hz]
            entry = bs(spot, k, dte / 365.0, sigma, "C")
            left = max(0, dte - (fwd * 7 / 5 if hz == "weekly" else fwd))
            exit_mid = bs(fut, k, left / 365.0, sigma, "C")
            rows.append(
                {
                    "leg": hz,
                    "day": str(df.index[i])[:10],
                    "underlying_pct": (fut - spot) / spot * 100.0,
                    "option_pct": round_trip_pct(entry, exit_mid),
                    "premium": entry,
                }
            )
    return rows


def _et_index(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None
    # the fetcher keeps naive ET wall time
    return frame.tz_convert(ET) if frame.index.tz is not None else frame.tz_localize(ET)


def power_hour_leg(
    symbol: str,
    bars: pd.DataFrame | None,
    daily: pd.DataFrame | None,
    qqq: pd.DataFrame | None = None,
) -> list[dict]:
    from odte_scanner.signals.power_hour import compute_vwap, decide_power_hour

    qqq = _et_index(qqq)

    rows: list[dict] = []
    if bars is None or bars.empty:
        return rows
    bars = bars.tz_convert(ET) if bars.index.tz is not None else bars.tz_localize(ET)  # fetcher keeps naive ET wall time
    for day, session in bars.groupby(bars.index.date):
        session = session.between_time("09:30", "15:59")
        if len(session) < 60:
            continue
        sigma = 0.4
        if daily is not None and not daily.empty:
            hist = daily.loc[: pd.Timestamp(day) - pd.Timedelta(days=1)]
            if len(hist) >= 20:
                sigma = realized_vol(hist["Close"])
        for minute in range(0, 46, 5):
            clock = pd.Timestamp(datetime.combine(day, time(15, minute)), tz=ET)
            seen = session[session.index < clock]
            if seen.empty:
                continue
            last = float(seen["Close"].iloc[-1])
            prior = seen[seen.index < clock - pd.Timedelta(minutes=15)]
            mom = (last / float(prior["Close"].iloc[-1]) - 1.0) * 100.0 if not prior.empty else None
            quote = {"last": last, "day_high": float(seen["High"].max()), "mom_15m_pct": mom}
            qqq_quote = qqq_vwap = None
            if qqq is not None:
                qday = qqq[(qqq.index.date == day) & (qqq.index < clock)]
                if not qday.empty:
                    qqq_quote = {"last": float(qday["Close"].iloc[-1])}
                    qqq_vwap = compute_vwap(qday, now=clock.to_pydatetime())
            sig = decide_power_hour(
                symbol,
                quote=quote,
                qqq_quote=qqq_quote,
                qqq_vwap=qqq_vwap,
                bars_1m=seen,
                phase="power_hour",
                now=clock.to_pydatetime(),
            )
            if sig.action not in {"LONG", "SHORT"}:
                continue
            right = "C" if sig.action == "LONG" else "P"
            k = strike_for(last)
            minutes_left = 60 - minute
            entry = bs(last, k, minutes_left / (60 * 6.5 * 252), sigma, right)
            after = session[session.index >= clock]
            flat = pd.Timestamp(datetime.combine(day, time(15, 50)), tz=ET)
            exit_px = None
            exit_at = flat
            for ts_, bar in after.iterrows():
                if ts_ >= flat:
                    break
                if sig.stop is not None:
                    if right == "C" and float(bar["Low"]) <= sig.stop:
                        exit_px, exit_at = float(sig.stop), ts_
                        break
                    if right == "P" and float(bar["High"]) >= sig.stop:
                        exit_px, exit_at = float(sig.stop), ts_
                        break
            if exit_px is None:
                tail = after[after.index < flat]
                exit_px = float(tail["Close"].iloc[-1]) if not tail.empty else last
            left_min = max(0.0, (pd.Timestamp(datetime.combine(day, time(16, 0)), tz=ET) - exit_at).total_seconds() / 60)
            exit_mid = bs(exit_px, k, left_min / (60 * 6.5 * 252), sigma, right)
            move = (exit_px - last) / last * 100.0 * (1 if right == "C" else -1)
            rows.append(
                {
                    "leg": "power_hour",
                    "day": str(day),
                    "underlying_pct": move,
                    "option_pct": round_trip_pct(entry, exit_mid),
                    "premium": entry,
                }
            )
            break
    return rows


def run_symbol(args: tuple) -> tuple[str, list[dict]]:
    symbol, weights = args
    try:
        df = fetch_history(symbol, period=DAILY_PERIOD)
        spy = fetch_history("SPY", period=DAILY_PERIOD)
        vix = fetch_history("^VIX", period=DAILY_PERIOD, yahoo_symbol="^VIX")
        rows = daily_leg(symbol, df, spy, vix, weights)
        try:
            bars = fetch_history(symbol, period="60d", interval="5m")
        except Exception:  # noqa: BLE001
            bars = None
        qqq = fetch_history("QQQ", period="60d", interval="5m")
        rows += power_hour_leg(symbol, bars, df, qqq)
        return symbol, rows
    except Exception as exc:  # noqa: BLE001
        return symbol, [{"error": str(exc)}]


def stats(rows: list[dict]) -> dict:
    rows = [r for r in rows if not math.isnan(r.get("option_pct", float("nan")))]
    if not rows:
        return {"trades": 0}
    opt = [r["option_pct"] for r in rows]
    und = [r["underlying_pct"] for r in rows]
    return {
        "trades": len(rows),
        "win_pct": round(100.0 * sum(1 for x in opt if x > 0) / len(opt), 1),
        "avg_option_pct": round(mean(opt), 2),
        "sum_option_R": round(sum(opt) / 100.0, 2),
        "underlying_win_pct": round(100.0 * sum(1 for x in und if x > 0) / len(und), 1),
        "avg_underlying_pct": round(mean(und), 3),
    }


def halves(rows: list[dict], split: dict[str, str]) -> dict:
    out = {}
    for leg in sorted({r["leg"] for r in rows if "leg" in r}):
        legs = [r for r in rows if r.get("leg") == leg]
        cut = split[leg]
        out[leg] = {
            "all": stats(legs),
            "first": stats([r for r in legs if r["day"] < cut]),
            "second": stats([r for r in legs if r["day"] >= cut]),
        }
    allr = [r for r in rows if "leg" in r]
    out["all_legs"] = {
        "all": stats(allr),
        "first": stats([r for r in allr if r["day"] < split[r["leg"]]]),
        "second": stats([r for r in allr if r["day"] >= split[r["leg"]]]),
    }
    return out


def main(candidates: list[str]) -> None:
    cfg = load_config()
    algo = cfg.get("algos") or {}
    weights = dict(algo.get("weights_by_horizon") or {})
    weights["_legacy"] = algo.get("weights") or {}
    base = [s for s in FOCUS_DEFAULT if s not in SKIP]
    names = list(dict.fromkeys(base + candidates))
    # warm the shared caches once so workers do not race on SPY / VIX
    fetch_history("SPY", period=DAILY_PERIOD)
    fetch_history("^VIX", period=DAILY_PERIOD, yahoo_symbol="^VIX")
    fetch_history("QQQ", period="60d", interval="5m")
    with ProcessPoolExecutor(max_workers=6) as pool:
        results = dict(pool.map(run_symbol, [(s, weights) for s in names]))
    all_rows = [r for rows in results.values() for r in rows if "leg" in r]
    split = {}
    for leg in {r["leg"] for r in all_rows}:
        days = sorted({r["day"] for r in all_rows if r["leg"] == leg})
        split[leg] = days[len(days) // 2]
    base_rows = [r for s in base for r in results.get(s, []) if "leg" in r]
    per_name = {s: halves([r for r in results.get(s, []) if "leg" in r], split) for s in names}
    per_name_avgs = {}
    for leg in list(split) + ["all_legs"]:
        for part in ("all", "first", "second"):
            vals = [
                per_name[s][leg][part]["avg_option_pct"]
                for s in base
                if leg in per_name[s] and per_name[s][leg][part].get("trades", 0) >= 5
            ]
            per_name_avgs.setdefault(leg, {})[part] = round(mean(vals), 2) if vals else None
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": __doc__,
        "costs": {
            "half_spread_pct": HALF_SPREAD_PCT,
            "half_spread_min_per_share": HALF_SPREAD_MIN,
            "commission_per_contract_each_side": 0.65,
        },
        "daily_period": DAILY_PERIOD,
        "split_day": split,
        "baseline_names": len(base),
        "baseline_pooled": halves(base_rows, split),
        "baseline_per_name_mean": per_name_avgs,
        "candidates": {s: per_name[s] for s in candidates},
        "errors": {s: rows[0]["error"] for s, rows in results.items() if rows and "error" in rows[0]},
        "baseline_per_name": {s: per_name[s] for s in base},
    }
    out = ROOT / "analysis" / f"focus_candidates_backtest_{DAILY_PERIOD}.json"
    out.write_text(json.dumps(payload, separators=(",", ":"), default=str))
    print(out)


if __name__ == "__main__":
    main([s.upper() for s in sys.argv[1:]] or ["HUBS", "DUOL", "CRM"])
