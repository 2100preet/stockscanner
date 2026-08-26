"""ORB15 Green-Friday put backtest for the 0DTE $1K sleeve.

Walks 1-minute (or 5-minute) RTH bars session-by-session:
  1. Form ORB15 High/Low from 09:30–09:45 ET
  2. IN (BUY PUT) on confirmed break + hold of ORB Low (green session default)
  3. OUT (SELL PUT) on 2-bar reclaim / flatten clock / target / stop

Option P&L is a transparent ATM 0DTE put *proxy* from underlying move
(elasticity model) — not live chain fills. Used to surface win-rate on the desk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from odte_scanner.challenge.orb15 import ORB_END, RTH_OPEN, compute_orb15, synthesize_1m_from_5m

ET = ZoneInfo("America/New_York")
FLATTEN = time(15, 45)
RTH_CLOSE = time(16, 0)
ENTRY_CUTOFF = time(14, 0)


@dataclass
class Odte1kBtTrade:
    symbol: str
    session_date: str
    entered_at: str
    exited_at: str
    entry_spot: float
    exit_spot: float
    orb_low: float
    orb_high: float
    green_friday: bool
    exit_reason: str
    underlying_ret_pct: float
    put_ret_pct: float
    win: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Odte1kBtResult:
    symbol: str
    sessions: int = 0
    signals: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_pct: float = 0.0
    avg_put_ret_pct: float = 0.0
    avg_under_ret_pct: float = 0.0
    expectancy_put_pct: float = 0.0
    hit_plus_40: float = 0.0
    hit_plus_80: float = 0.0
    max_dd_pct: float = 0.0
    final_equity: float = 1.0
    equity_curve: list[float] = field(default_factory=list)
    trade_rows: list[Odte1kBtTrade] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_rows"] = [t.to_dict() if hasattr(t, "to_dict") else t for t in self.trade_rows]
        return d


def _to_et(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    idx = out.index
    if getattr(idx, "tz", None) is None:
        try:
            out.index = pd.to_datetime(idx, utc=True).tz_convert(ET)
        except Exception:  # noqa: BLE001
            out.index = pd.to_datetime(idx).tz_localize(ET, ambiguous="infer", nonexistent="shift_forward")
    else:
        out.index = idx.tz_convert(ET)
    return out


def _put_proxy_ret(
    entry_spot: float,
    exit_spot: float,
    *,
    elasticity: float = 25.0,
    floor: float = -0.92,
    cap: float = 3.0,
) -> float:
    """Map underlying move → ATM 0DTE put return proxy.

    elasticity≈25 ≈ ATM 0DTE: −0.5% tape ≈ +12.5% put; −1.5% dump ≈ +37.5%.
    """
    if entry_spot <= 0:
        return 0.0
    under = (exit_spot - entry_spot) / entry_spot
    put = -under * float(elasticity)
    return float(np.clip(put, floor, cap))


def _session_green(day_bars: pd.DataFrame, orb_open: float | None) -> bool:
    if day_bars is None or day_bars.empty:
        return False
    last = float(day_bars["Close"].iloc[-1])
    open_px = float(orb_open) if orb_open is not None else float(day_bars["Open"].iloc[0])
    return last > open_px


def backtest_orb15_puts(
    bars: pd.DataFrame | None,
    *,
    symbol: str = "SPY",
    buffer_usd: float = 0.05,
    reclaim_buffer_usd: float = 0.35,
    hold_bars: int = 2,
    require_green: bool = True,
    flatten_et: time = FLATTEN,
    target_put_pct: float = 0.80,
    stop_put_pct: float = -0.45,
    elasticity: float = 25.0,
    max_trades_per_day: int = 2,
    starting_equity: float = 1.0,
    risk_frac: float = 0.85,
    entry_cutoff: time = ENTRY_CUTOFF,
) -> Odte1kBtResult:
    """Session walk-forward ORB15 put backtest on 1m/5m OHLCV bars."""
    result = Odte1kBtResult(symbol=str(symbol).upper(), equity_curve=[starting_equity])
    if bars is None or bars.empty:
        result.note = "No bars for backtest"
        return result

    df = _to_et(bars)
    if df is None or df.empty or "Close" not in df.columns:
        result.note = "Bars missing Close after TZ normalize"
        return result

    if "High" not in df.columns:
        df["High"] = df["Close"]
    if "Low" not in df.columns:
        df["Low"] = df["Close"]
    if "Open" not in df.columns:
        df["Open"] = df["Close"]

    # Expand 5m → pseudo-1m so ORB window + hold_bars stay consistent
    try:
        gaps = df.index.to_series().diff().dt.total_seconds().dropna()
        med = float(gaps.median()) if len(gaps) else 60.0
        if med >= 240:
            syn = synthesize_1m_from_5m(df)
            if syn is not None and not syn.empty:
                df = _to_et(syn)
    except Exception:  # noqa: BLE001
        pass

    equity = float(starting_equity)
    peak = equity
    max_dd = 0.0
    sessions = 0
    signals = 0

    days = sorted({d.date() if hasattr(d, "date") else d for d in df.index})
    for day in days:
        day_mask = df.index.date == day
        day_bars = df.loc[day_mask]
        if day_bars.empty:
            continue
        start = datetime.combine(day, RTH_OPEN, tzinfo=ET)
        end = datetime.combine(day, ORB_END, tzinfo=ET)
        flat_dt = datetime.combine(day, flatten_et, tzinfo=ET)
        close_dt = datetime.combine(day, RTH_CLOSE, tzinfo=ET)
        orb_slice = day_bars.loc[(day_bars.index >= start) & (day_bars.index < end)]
        if len(orb_slice) < 3:
            continue
        sessions += 1
        orb = compute_orb15(
            day_bars,
            symbol=symbol,
            session_day=day if isinstance(day, date) else day,
            now=end + timedelta(minutes=1),
        )
        if orb.status != "ready" or orb.low is None or orb.high is None:
            continue

        post = day_bars.loc[(day_bars.index >= end) & (day_bars.index <= close_dt)]
        if post.empty:
            continue

        green = _session_green(orb_slice, orb.open)
        if require_green and not green:
            continue

        trades_today = 0
        i = 0
        closes = post["Close"].astype(float)
        lows = post["Low"].astype(float)
        idx = list(post.index)

        while i < len(idx) and trades_today < max_trades_per_day:
            ts = idx[i]
            if ts.time() >= flatten_et or ts.time() >= entry_cutoff:
                break
            window = closes.iloc[max(0, i - hold_bars + 1) : i + 1]
            if len(window) < hold_bars:
                i += 1
                continue
            broke_hold = bool((window < (float(orb.low) - buffer_usd)).all())
            wick_break = float(lows.iloc[i]) < (float(orb.low) - buffer_usd)
            close_hold = float(closes.iloc[i]) <= float(orb.low)
            trigger = broke_hold or (wick_break and close_hold and i >= hold_bars - 1)
            if not trigger:
                i += 1
                continue

            signals += 1
            entry_spot = float(closes.iloc[i])
            entry_ts = ts
            exit_spot = entry_spot
            exit_ts = entry_ts
            exit_reason = "session_end"
            put_ret = 0.0
            reclaim_streak = 0
            for j in range(i + 1, len(idx)):
                px = float(closes.iloc[j])
                ts_j = idx[j]
                put_ret = _put_proxy_ret(entry_spot, px, elasticity=elasticity)
                exit_spot = px
                exit_ts = ts_j
                if put_ret >= target_put_pct:
                    exit_reason = "target"
                    break
                if put_ret <= stop_put_pct:
                    exit_reason = "stop"
                    break
                if px > float(orb.low) + reclaim_buffer_usd:
                    reclaim_streak += 1
                    if reclaim_streak >= 2:
                        exit_reason = "reclaim_orb_low"
                        break
                else:
                    reclaim_streak = 0
                if ts_j.time() >= flatten_et:
                    exit_reason = "flatten"
                    break
            else:
                if exit_ts.time() >= flatten_et or exit_ts >= flat_dt:
                    exit_reason = "flatten"
                else:
                    exit_reason = "session_end"

            put_ret = _put_proxy_ret(entry_spot, exit_spot, elasticity=elasticity)
            under_ret = (exit_spot - entry_spot) / entry_spot * 100.0
            win = put_ret > 0
            trade = Odte1kBtTrade(
                symbol=str(symbol).upper(),
                session_date=day.isoformat() if hasattr(day, "isoformat") else str(day),
                entered_at=entry_ts.isoformat(),
                exited_at=exit_ts.isoformat(),
                entry_spot=round(entry_spot, 4),
                exit_spot=round(exit_spot, 4),
                orb_low=float(orb.low),
                orb_high=float(orb.high),
                green_friday=green,
                exit_reason=exit_reason,
                underlying_ret_pct=round(under_ret, 3),
                put_ret_pct=round(put_ret * 100.0, 2),
                win=win,
            )
            result.trade_rows.append(trade)
            trades_today += 1
            equity = equity * (1.0 + risk_frac * put_ret)
            result.equity_curve.append(round(equity, 4))
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            i = max(i + 1, next((k for k, t in enumerate(idx) if t >= exit_ts), i + 1) + 1)

    trades = result.trade_rows
    n = len(trades)
    wins = sum(1 for t in trades if t.win)
    put_rets = [t.put_ret_pct for t in trades]
    under_rets = [t.underlying_ret_pct for t in trades]
    result.sessions = sessions
    result.signals = signals
    result.trades = n
    result.wins = wins
    result.losses = n - wins
    result.win_pct = round((wins / n) * 100.0, 1) if n else 0.0
    result.avg_put_ret_pct = round(float(np.mean(put_rets)), 2) if put_rets else 0.0
    result.avg_under_ret_pct = round(float(np.mean(under_rets)), 3) if under_rets else 0.0
    result.expectancy_put_pct = result.avg_put_ret_pct
    result.hit_plus_40 = round(sum(1 for r in put_rets if r >= 40) / n * 100.0, 1) if n else 0.0
    result.hit_plus_80 = round(sum(1 for r in put_rets if r >= 80) / n * 100.0, 1) if n else 0.0
    result.max_dd_pct = round(max_dd * 100.0, 2)
    result.final_equity = round(equity, 4)
    result.note = (
        f"ORB15 put proxy · elasticity={elasticity} · hold_bars={hold_bars} · "
        f"green_filter={'on' if require_green else 'off'} · cutoff={entry_cutoff.strftime('%H:%M')}"
    )
    return result


def backtest_odte_1k_universe(
    bar_map: dict[str, pd.DataFrame],
    *,
    symbols: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run ORB15 put backtest across symbols; return desk summary + per-name stats."""
    syms = symbols or list(bar_map.keys())
    per: dict[str, Any] = {}
    all_trades: list[dict[str, Any]] = []
    for sym in syms:
        bars = bar_map.get(sym)
        if bars is None or (hasattr(bars, "empty") and bars.empty):
            continue
        r = backtest_orb15_puts(bars, symbol=sym, **kwargs)
        per[sym] = {k: v for k, v in r.to_dict().items() if k != "trade_rows"}
        all_trades.extend(r.to_dict().get("trade_rows") or [])

    n = len(all_trades)
    wins = sum(1 for t in all_trades if t.get("win"))
    put_rets = [float(t.get("put_ret_pct") or 0) for t in all_trades]
    return {
        "desk": "odte_1k_backtest",
        "symbols": list(per.keys()),
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_pct": round((wins / n) * 100.0, 1) if n else 0.0,
        "avg_put_ret_pct": round(float(np.mean(put_rets)), 2) if put_rets else 0.0,
        "hit_plus_40": round(sum(1 for r in put_rets if r >= 40) / n * 100.0, 1) if n else 0.0,
        "hit_plus_80": round(sum(1 for r in put_rets if r >= 80) / n * 100.0, 1) if n else 0.0,
        "by_symbol": per,
        "sample_trades": all_trades[-12:],
        "disclaimer": (
            "Backtest uses ATM 0DTE put *proxy* from underlying elasticity — "
            "not live option fills. Research only. Default: green ORB + confirmed hold + "
            "2-bar reclaim OUT + no new IN after 14:00 ET."
        ),
    }


def fetch_bars_for_backtest(
    symbol: str,
    *,
    yahoo_symbol: str | None = None,
    period: str = "7d",
    interval: str = "1m",
) -> pd.DataFrame | None:
    """Best-effort Yahoo bars for ORB15 backtest (1m capped ~7d)."""
    try:
        import yfinance as yf

        t = yf.Ticker(yahoo_symbol or symbol)
        df = t.history(period=period, interval=interval, prepost=False, auto_adjust=False)
        if df is None or df.empty:
            df = t.history(period="60d", interval="5m", prepost=False, auto_adjust=False)
        return df if df is not None and not df.empty else None
    except Exception:  # noqa: BLE001
        return None
