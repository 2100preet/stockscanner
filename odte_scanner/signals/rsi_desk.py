"""RSI desk — classic Wilder RSI(14) oversold / overbought scan.

Classic reading (with caveats):
  RSI ≤ 30  → oversold → BUY bias (mean-reversion long watch)
  RSI ≥ 70  → overbought → SELL bias (fade / take-profit watch)
  30–70     → NEUTRAL / WATCH

RSI alone is not an entry trigger — strong trends can stay oversold or
overbought for weeks. Prefer rising RSI off ≤30 for buys, falling RSI
off ≥70 for sells. Pair with price structure / VWAP desks.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = 14
OVERSOLD = 30.0
OVERBOUGHT = 70.0
DEEP_OVERSOLD = 25.0
DEEP_OVERBOUGHT = 75.0


@dataclass
class RsiSignal:
    symbol: str
    rsi: float | None
    prev_rsi: float | None
    zone: str  # oversold | approaching_oversold | neutral | approaching_overbought | overbought | unknown
    action: str  # BUY | SELL | WATCH | WAIT
    strength: float
    rising: bool | None
    last: float | None
    change_pct: float | None
    detail: str
    trigger: str
    risk_line: str
    period: int = DEFAULT_PERIOD
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def wilder_rsi(close: pd.Series, period: int = DEFAULT_PERIOD) -> pd.Series:
    """Wilder RSI via EWM (alpha = 1/period) — industry-standard RSI(14)."""
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    # Zero loss with positive gain → RSI 100; both zero → 50
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return rsi


def classify_rsi(
    rsi: float | None,
    *,
    prev_rsi: float | None = None,
    oversold: float = OVERSOLD,
    overbought: float = OVERBOUGHT,
) -> tuple[str, str, float, str, list[str]]:
    """Return zone, action, strength, detail, reasons."""
    if rsi is None or (isinstance(rsi, float) and np.isnan(rsi)):
        return "unknown", "WAIT", 20.0, "RSI unavailable", ["Insufficient bars"]

    rising = prev_rsi is not None and rsi > prev_rsi
    falling = prev_rsi is not None and rsi < prev_rsi
    reasons: list[str] = [f"RSI(14) = {rsi:.1f}"]

    if rsi <= DEEP_OVERSOLD:
        zone = "oversold"
        action = "BUY"
        strength = 88.0 if rising else 78.0
        detail = f"Deep oversold RSI {rsi:.1f} ≤ {DEEP_OVERSOLD:.0f} — BUY bias"
        reasons.append("Deep oversold")
        if rising:
            reasons.append("RSI turning up off lows")
        else:
            reasons.append("Still falling — size small / wait for tick up")
    elif rsi <= oversold:
        zone = "oversold"
        action = "BUY"
        strength = 82.0 if rising else 72.0
        detail = f"Oversold RSI {rsi:.1f} ≤ {oversold:.0f} — BUY bias"
        reasons.append("Oversold zone")
        if rising:
            reasons.append("Rising through oversold — classic bounce setup")
    elif rsi < 40:
        zone = "approaching_oversold"
        action = "WATCH"
        strength = 55.0 if rising else 48.0
        detail = f"Approaching oversold ({rsi:.1f}) — WATCH for ≤{oversold:.0f}"
        reasons.append("Near oversold — not a buy yet")
    elif rsi >= DEEP_OVERBOUGHT:
        zone = "overbought"
        action = "SELL"
        strength = 88.0 if falling else 78.0
        detail = f"Deep overbought RSI {rsi:.1f} ≥ {DEEP_OVERBOUGHT:.0f} — SELL bias"
        reasons.append("Deep overbought")
        if falling:
            reasons.append("RSI rolling over from highs")
        else:
            reasons.append("Still rising — trend may extend; fade carefully")
    elif rsi >= overbought:
        zone = "overbought"
        action = "SELL"
        strength = 82.0 if falling else 72.0
        detail = f"Overbought RSI {rsi:.1f} ≥ {overbought:.0f} — SELL bias"
        reasons.append("Overbought zone")
        if falling:
            reasons.append("Falling through overbought — classic fade setup")
    elif rsi > 60:
        zone = "approaching_overbought"
        action = "WATCH"
        strength = 55.0 if falling else 48.0
        detail = f"Approaching overbought ({rsi:.1f}) — WATCH for ≥{overbought:.0f}"
        reasons.append("Near overbought — not a sell yet")
    else:
        zone = "neutral"
        action = "WATCH"
        strength = 40.0
        detail = f"Neutral RSI {rsi:.1f} — no oversold/overbought edge"
        reasons.append("Mid-range RSI — stand aside on RSI alone")

    if rising is True:
        reasons.append("RSI rising")
    elif falling is True:
        reasons.append("RSI falling")

    return zone, action, strength, detail, reasons


def decide_rsi(
    symbol: str,
    bars: pd.DataFrame | None,
    *,
    period: int = DEFAULT_PERIOD,
    oversold: float = OVERSOLD,
    overbought: float = OVERBOUGHT,
    quote: dict[str, Any] | None = None,
) -> RsiSignal:
    sym = str(symbol).upper()
    q = quote or {}
    last = None
    change_pct = None
    if q.get("last") is not None:
        try:
            last = float(q["last"])
        except (TypeError, ValueError):
            last = None
    if q.get("session_change_pct") is not None:
        try:
            change_pct = float(q["session_change_pct"])
        except (TypeError, ValueError):
            change_pct = None

    rsi_val = prev = None
    rising = None
    if bars is not None and not bars.empty and "Close" in bars.columns and len(bars) >= period + 2:
        series = wilder_rsi(bars["Close"], period=period)
        cur = series.iloc[-1]
        prv = series.iloc[-2] if len(series) >= 2 else cur
        if not np.isnan(cur):
            rsi_val = float(cur)
        if not np.isnan(prv):
            prev = float(prv)
        if rsi_val is not None and prev is not None:
            rising = rsi_val > prev
        if last is None:
            try:
                last = float(bars["Close"].iloc[-1])
            except Exception:  # noqa: BLE001
                pass
        if change_pct is None and len(bars) >= 2:
            try:
                c0, c1 = float(bars["Close"].iloc[-2]), float(bars["Close"].iloc[-1])
                if c0:
                    change_pct = (c1 / c0 - 1.0) * 100.0
            except Exception:  # noqa: BLE001
                pass

    zone, action, strength, detail, reasons = classify_rsi(
        rsi_val, prev_rsi=prev, oversold=oversold, overbought=overbought
    )
    trigger = (
        f"BUY when RSI(14) ≤ {oversold:.0f} (oversold); "
        f"SELL when RSI(14) ≥ {overbought:.0f} (overbought)"
    )
    risk_line = (
        "RSI is a bias, not a standalone entry — confirm with price/VWAP; "
        "trends can stay oversold/overbought · never average down blindly"
    )
    return RsiSignal(
        symbol=sym,
        rsi=round(rsi_val, 2) if rsi_val is not None else None,
        prev_rsi=round(prev, 2) if prev is not None else None,
        zone=zone,
        action=action,
        strength=round(strength, 1),
        rising=rising,
        last=round(last, 4) if last is not None else None,
        change_pct=round(change_pct, 3) if change_pct is not None else None,
        detail=detail,
        trigger=trigger,
        risk_line=risk_line,
        period=period,
        reasons=reasons,
    )


def resolve_rsi_symbols(
    symbols: list[str] | str | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> list[str]:
    cfg = config or {}
    raw = symbols
    if raw is None:
        raw = (cfg.get("actions") or {}).get("rsi_desk_symbols", "focus")
    if isinstance(raw, str) and raw.strip().lower() in {"", "focus", "all", "tickers"}:
        raw = list(cfg.get("tickers") or [])
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        from odte_scanner.data.universe import FOCUS_DEFAULT

        raw = list(FOCUS_DEFAULT)

    # Sticky / Friday-close names first so rate limits don't drop them
    priority = (
        "SOFI", "SPCX", "AVGO", "COST", "ASTS", "IREN", "CRWV", "IBIT", "MRNA",
        "SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMZN", "META", "GOOGL", "AMD",
    )
    provided = [str(s).replace(".", "-").upper() for s in raw if str(s).strip()]
    provided_set = set(provided)
    seen: set[str] = set()
    out: list[str] = []
    for sym in list(priority) + provided:
        if sym not in provided_set:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def build_rsi_board(
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    symbols: list[str] | str | None = None,
    config: dict[str, Any] | None = None,
    bars_map: dict[str, pd.DataFrame] | None = None,
    fetch_bars: bool = True,
    max_bar_fetch: int = 60,
    aliases: dict[str, str] | None = None,
    period: int = DEFAULT_PERIOD,
    oversold: float = OVERSOLD,
    overbought: float = OVERBOUGHT,
    cache_only: bool = False,
) -> dict[str, Any]:
    """Build RSI oversold/overbought board for focus (or provided) symbols."""
    from odte_scanner.data.fetcher import fetch_history

    quotes = quotes or {}
    aliases = aliases or {}
    bars_map = dict(bars_map or {})
    symbols = resolve_rsi_symbols(symbols, config=config)
    cfg = config or {}
    actions = cfg.get("actions") or {}
    period = int(actions.get("rsi_period", period))
    oversold = float(actions.get("rsi_oversold", oversold))
    overbought = float(actions.get("rsi_overbought", overbought))
    max_bar_fetch = int(actions.get("rsi_desk_max_bar_fetch", max_bar_fetch))

    fetch_list = symbols[: max(0, max_bar_fetch)]
    if fetch_bars:
        for s in fetch_list:
            if s in bars_map and bars_map[s] is not None and not bars_map[s].empty:
                continue
            try:
                df = fetch_history(
                    s,
                    period="6mo",
                    interval="1d",
                    yahoo_symbol=aliases.get(s),
                    use_cache=True,
                    cache_max_age_hours=6 if not cache_only else 72,
                )
                if cache_only and (df is None or df.empty):
                    continue
                if df is not None and not df.empty:
                    bars_map[s] = df
            except Exception as exc:  # noqa: BLE001
                logger.debug("RSI bars failed for %s: %s", s, exc)

    signals: list[RsiSignal] = []
    for sym in symbols:
        sig = decide_rsi(
            sym,
            bars_map.get(sym),
            period=period,
            oversold=oversold,
            overbought=overbought,
            quote=quotes.get(sym),
        )
        signals.append(sig)

    buys = [s for s in signals if s.action == "BUY"]
    sells = [s for s in signals if s.action == "SELL"]
    watches = [s for s in signals if s.action in {"WATCH", "WAIT"}]
    buys.sort(key=lambda s: (s.rsi is None, s.rsi if s.rsi is not None else 999, -s.strength))
    sells.sort(key=lambda s: (s.rsi is None, -(s.rsi or 0), -s.strength))
    all_sorted = sorted(
        signals,
        key=lambda s: (
            0 if s.action == "BUY" else 1 if s.action == "SELL" else 2,
            s.rsi if s.rsi is not None else 50,
        ),
    )

    primary = None
    for pool in (buys, sells, watches):
        if pool:
            primary = pool[0]
            break

    return {
        "desk": "rsi",
        "title": "RSI Desk — Oversold BUY · Overbought SELL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": period,
        "oversold": oversold,
        "overbought": overbought,
        "symbols": symbols,
        "primary": primary.to_dict() if primary else None,
        "buy": [s.to_dict() for s in buys],
        "sell": [s.to_dict() for s in sells],
        "watch": [s.to_dict() for s in watches],
        "all": [s.to_dict() for s in all_sorted],
        "counts": {
            "buy": len(buys),
            "sell": len(sells),
            "watch": len(watches),
            "oversold": sum(1 for s in signals if s.zone == "oversold"),
            "overbought": sum(1 for s in signals if s.zone == "overbought"),
            "names": len(symbols),
            "with_rsi": sum(1 for s in signals if s.rsi is not None),
        },
        "playbook": [
            f"RSI({period}) Wilder on daily bars.",
            f"BUY bias when RSI ≤ {oversold:.0f} (oversold) — stronger if RSI is rising off the low.",
            f"SELL bias when RSI ≥ {overbought:.0f} (overbought) — stronger if RSI is rolling over.",
            "RSI mid-range (≈40–60) = WATCH — no mean-reversion edge from RSI alone.",
            "Caveat: strong trends can stay oversold or overbought for weeks — confirm with price/VWAP.",
            "This desk is educational bias only — not auto BUY NOW / SELL NOW.",
        ],
        "disclaimer": (
            "Educational / research only. Daily RSI from Yahoo bars (cached). "
            "Oversold ≠ guaranteed bounce; overbought ≠ guaranteed dump. Not financial advice."
        ),
    }
