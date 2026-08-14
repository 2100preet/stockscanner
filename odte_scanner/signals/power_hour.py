"""Power Hour desk — 15m VWAP triggers with LONG / SHORT / WAIT.

Named playbooks (user rules):
  NU   — Long on 15m close above VWAP then break of that candle’s high;
         risk: exit on close back below VWAP
  NVDA — Long only if QQQ also above VWAP and NVDA forms a higher low;
         stop below the higher-low candle
  CAPR — Momentum only after a tight 15m base breaks with volume;
         strict stop below base low (no average down)
  ETON — Prefer pullback that holds VWAP, then 15m bullish reclaim;
         stop below VWAP / pullback low
  HTFL — Breakout after consolidation near HOD, not immediately after a spike
  NXPI — Semi: QQQ ≥ VWAP + 15m HL / VWAP reclaim (no spike chase);
         stop below HL / reclaim candle

Generic sleeve (TSLA + full focus list): LONG above VWAP with bullish 15m
structure; SHORT below VWAP with bearish 15m structure; else WAIT.

Power hour window: 15:00–16:00 America/New_York (prep from 14:30).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from odte_scanner.time_cst import signal_timestamps

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

POWER_HOUR_START = time(15, 0)
POWER_HOUR_END = time(16, 0)
PREP_START = time(14, 30)

# Explicit playbook cards always shown first
SPECIAL_TICKERS = ("NU", "NVDA", "CAPR", "ETON", "HTFL", "TSLA", "GOOGL", "NXPI")

SPECIAL_RULES: dict[str, dict[str, str]] = {
    "NU": {
        "bias": "LONG",
        "trigger": "Long on a 15-minute close above VWAP, then a break of that candle’s high",
        "risk": "Exit on a close back below VWAP",
    },
    "NVDA": {
        "bias": "LONG",
        "trigger": "Long only if QQQ is also above VWAP and NVDA forms a higher low",
        "risk": "Stop below the higher-low candle",
    },
    "CAPR": {
        "bias": "LONG",
        "trigger": "Momentum only after a tight 15-minute base breaks with volume",
        "risk": "Strict stop below base low; do not average down",
    },
    "ETON": {
        "bias": "LONG",
        "trigger": "Prefer a pullback that holds VWAP, then a 15-minute bullish reclaim",
        "risk": "Stop below VWAP / pullback low",
    },
    "HTFL": {
        "bias": "LONG",
        "trigger": "Breakout after consolidation near HOD, not immediately after a spike",
        "risk": "Stop below consolidation / breakout candle low",
    },
    "TSLA": {
        "bias": "BOTH",
        "trigger": "Power-hour LONG above VWAP with bullish 15m structure; SHORT below VWAP with bearish 15m (QQQ breadth preferred)",
        "risk": "Stop beyond the signal 15m candle (long: below low · short: above high); no average down",
    },
    "GOOGL": {
        "bias": "LONG",
        "trigger": "Long only if QQQ is also above VWAP and GOOGL holds a 15m higher low / VWAP reclaim",
        "risk": "Stop below the higher-low / reclaim candle; no chase after a spike",
    },
    "NXPI": {
        "bias": "LONG",
        "trigger": "Semi long only if QQQ ≥ VWAP and NXPI holds a 15m higher low / VWAP reclaim (no spike)",
        "risk": "Stop below the higher-low / reclaim candle; never average down",
    },
}

# Mega-cap / high-beta names that should not long against QQQ weakness
MEGA_BREADTH = {
    "GOOGL",
    "GOOG",
    "META",
    "AMZN",
    "MSFT",
    "AAPL",
    "AVGO",
    "AMD",
    "TSLA",
    "NFLX",
    "CRM",
    "NVDA",
    "ARM",
    "PLTR",
    "NXPI",
    "QCOM",
    "MU",
    "AMAT",
    "TSM",
}


@dataclass
class PowerHourSignal:
    action: str  # LONG | SHORT | WAIT | WATCH
    symbol: str
    strength: float
    headline: str
    detail: str
    trigger: str
    risk_line: str
    playbook: str = "generic"
    session_phase: str = "unknown"  # prep | power_hour | regular | closed
    last: float | None = None
    vwap: float | None = None
    vs_vwap_pct: float | None = None
    qqq_above_vwap: bool | None = None
    day_high: float | None = None
    dist_from_hod_pct: float | None = None
    mom_15m_pct: float | None = None
    candle_high: float | None = None
    candle_low: float | None = None
    stop: float | None = None
    special: bool = False
    signaled_at: str | None = None
    signaled_at_cst: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons or [])
        if self.action in {"LONG", "SHORT"} and not d.get("signaled_at"):
            d.update(signal_timestamps())
        return d


def session_phase(now: datetime | None = None) -> str:
    now_et = now.astimezone(ET) if now and now.tzinfo else (now.replace(tzinfo=ET) if now else datetime.now(ET))
    t = now_et.time()
    if PREP_START <= t < POWER_HOUR_START:
        return "prep"
    if POWER_HOUR_START <= t < POWER_HOUR_END:
        return "power_hour"
    if time(9, 30) <= t < PREP_START:
        return "regular"
    if t >= POWER_HOUR_END:
        return "closed"
    return "premarket"


def _to_et(df: pd.DataFrame) -> pd.DataFrame:
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


def compute_vwap(bars_1m: pd.DataFrame | None) -> float | None:
    if bars_1m is None or bars_1m.empty:
        return None
    df = _to_et(bars_1m)
    if "Close" not in df.columns:
        return None
    vol = df["Volume"] if "Volume" in df.columns else pd.Series(1.0, index=df.index)
    vol = vol.fillna(0).clip(lower=0)
    if float(vol.sum() or 0) <= 0:
        # Equal-weight typical price
        typ = df["Close"]
        if "High" in df.columns and "Low" in df.columns:
            typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
        return float(typ.mean()) if len(typ) else None
    typ = df["Close"]
    if "High" in df.columns and "Low" in df.columns:
        typ = (df["High"] + df["Low"] + df["Close"]) / 3.0
    return float((typ * vol).sum() / vol.sum())


def resample_15m(bars_1m: pd.DataFrame | None) -> pd.DataFrame | None:
    if bars_1m is None or bars_1m.empty:
        return None
    df = _to_et(bars_1m)
    ohlc = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    cols = {k: v for k, v in ohlc.items() if k in df.columns}
    if "Close" not in cols:
        return None
    try:
        out = df.resample("15min", label="left", closed="left").agg(cols).dropna(subset=["Close"])
        return out if not out.empty else None
    except Exception:  # noqa: BLE001
        return None


def fetch_intraday_1m(symbol: str, *, yahoo_symbol: str | None = None) -> pd.DataFrame | None:
    try:
        import yfinance as yf

        t = yf.Ticker(yahoo_symbol or symbol)
        df = t.history(period="1d", interval="1m", prepost=False, auto_adjust=False)
        if df is None or df.empty:
            df = t.history(period="5d", interval="1m", prepost=False, auto_adjust=False)
        return df if df is not None and not df.empty else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("power hour bars %s: %s", symbol, exc)
        return None


def _structure_from_15m(bars_15: pd.DataFrame | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "higher_low": False,
        "lower_high": False,
        "tight_base": False,
        "base_low": None,
        "base_high": None,
        "break_above_base": False,
        "break_below_base": False,
        "volume_expand": False,
        "last_close": None,
        "last_high": None,
        "last_low": None,
        "prev_close": None,
        "bullish_reclaim": False,
        "bearish_fail": False,
        "near_hod": False,
        "spike_risk": False,
    }
    if bars_15 is None or len(bars_15) < 3:
        return out
    closes = bars_15["Close"].astype(float)
    highs = bars_15["High"].astype(float) if "High" in bars_15.columns else closes
    lows = bars_15["Low"].astype(float) if "Low" in bars_15.columns else closes
    vols = bars_15["Volume"].astype(float) if "Volume" in bars_15.columns else pd.Series(0.0, index=bars_15.index)

    out["last_close"] = float(closes.iloc[-1])
    out["last_high"] = float(highs.iloc[-1])
    out["last_low"] = float(lows.iloc[-1])
    out["prev_close"] = float(closes.iloc[-2])

    # Higher low: last swing low > prior swing low
    if len(lows) >= 4:
        out["higher_low"] = float(lows.iloc[-1]) > float(lows.iloc[-3]) and float(closes.iloc[-1]) >= float(
            closes.iloc[-2]
        )
        out["lower_high"] = float(highs.iloc[-1]) < float(highs.iloc[-3]) and float(closes.iloc[-1]) <= float(
            closes.iloc[-2]
        )

    # Tight base = last 3 bars range small vs prior average range
    recent = bars_15.iloc[-4:-1] if len(bars_15) >= 4 else bars_15.iloc[:-1]
    if len(recent) >= 2 and "High" in recent.columns:
        base_high = float(recent["High"].max())
        base_low = float(recent["Low"].min())
        out["base_high"] = base_high
        out["base_low"] = base_low
        width = (base_high - base_low) / max(base_low, 1e-6) * 100
        out["tight_base"] = width <= 1.2  # ≤1.2% range
        last = float(closes.iloc[-1])
        out["break_above_base"] = last > base_high
        out["break_below_base"] = last < base_low

    if len(vols) >= 4 and float(vols.iloc[-4:-1].mean() or 0) > 0:
        out["volume_expand"] = float(vols.iloc[-1]) >= 1.25 * float(vols.iloc[-4:-1].mean())

    # Bullish reclaim: prior bar closed below VWAP proxy (prev), last closes strong
    out["bullish_reclaim"] = float(closes.iloc[-1]) > float(closes.iloc[-2]) and float(closes.iloc[-1]) >= float(
        highs.iloc[-2]
    ) * 0.998
    out["bearish_fail"] = float(closes.iloc[-1]) < float(closes.iloc[-2]) and float(closes.iloc[-1]) <= float(
        lows.iloc[-2]
    ) * 1.002

    # Spike = last bar range >> median range
    if "High" in bars_15.columns and len(bars_15) >= 6:
        ranges = (highs - lows).iloc[-8:-1]
        med = float(ranges.median()) if len(ranges) else 0
        last_rng = float(highs.iloc[-1] - lows.iloc[-1])
        out["spike_risk"] = med > 0 and last_rng >= 2.2 * med

    return out


def _quote_vwap_proxy(quote: dict[str, Any] | None) -> float | None:
    q = quote or {}
    for k in ("vwap", "VWAP"):
        if q.get(k) is not None:
            try:
                return float(q[k])
            except Exception:  # noqa: BLE001
                pass
    # Mid of day range as weak proxy when VWAP missing
    hi, lo = q.get("day_high"), q.get("day_low")
    if hi is not None and lo is not None:
        try:
            return (float(hi) + float(lo)) / 2.0
        except Exception:  # noqa: BLE001
            return None
    return None


def decide_power_hour(
    symbol: str,
    *,
    quote: dict[str, Any] | None = None,
    bars_1m: pd.DataFrame | None = None,
    qqq_quote: dict[str, Any] | None = None,
    qqq_vwap: float | None = None,
    phase: str | None = None,
    now: datetime | None = None,
) -> PowerHourSignal:
    sym = str(symbol).upper()
    q = quote or {}
    phase = phase or session_phase(now)
    special = SPECIAL_RULES.get(sym)
    playbook = sym if special else "generic"
    rule_trigger = (special or {}).get("trigger") or (
        "LONG if above VWAP + bullish 15m structure; SHORT if below VWAP + bearish 15m structure"
    )
    rule_risk = (special or {}).get("risk") or "Stop beyond the signal 15m candle; no averaging into losers"

    last = q.get("last")
    if last is None:
        last = q.get("live_last")
    last_f = float(last) if last is not None else None
    day_high = float(q["day_high"]) if q.get("day_high") is not None else None
    mom15 = float(q["mom_15m_pct"]) if q.get("mom_15m_pct") is not None else None

    vwap = compute_vwap(bars_1m) if bars_1m is not None else None
    if vwap is None:
        vwap = _quote_vwap_proxy(q)
    bars_15 = resample_15m(bars_1m) if bars_1m is not None else None
    st = _structure_from_15m(bars_15)

    vs_vwap = None
    above = below = False
    if last_f is not None and vwap is not None and vwap > 0:
        vs_vwap = (last_f / vwap - 1.0) * 100.0
        above = last_f >= vwap
        below = last_f < vwap

    dist_hod = None
    if last_f is not None and day_high:
        dist_hod = (last_f / day_high - 1.0) * 100.0

    qqq_above = None
    if qqq_vwap is not None and qqq_quote and qqq_quote.get("last") is not None:
        qqq_above = float(qqq_quote["last"]) >= float(qqq_vwap)
    elif qqq_quote:
        qv = _quote_vwap_proxy(qqq_quote)
        if qv is not None and qqq_quote.get("last") is not None:
            qqq_above = float(qqq_quote["last"]) >= float(qv)

    reasons: list[str] = []
    if phase == "prep":
        reasons.append("Power-hour prep (14:30–15:00 ET)")
    elif phase == "power_hour":
        reasons.append("Power hour (15:00–16:00 ET)")
    elif phase == "closed":
        reasons.append("RTH closed — levels for next session prep")

    action = "WAIT"
    strength = 35.0
    stop = None
    candle_high = st.get("last_high")
    candle_low = st.get("last_low")
    detail = ""

    # --- Special playbooks ---
    if sym == "NU":
        # Long: 15m close above VWAP then break of that candle high
        trigger_ok = above and (
            (st.get("break_above_base") and st.get("prev_close") is not None and vwap is not None and float(st["prev_close"]) >= vwap)
            or (mom15 is not None and mom15 > 0.05 and above and st.get("bullish_reclaim"))
            or (above and candle_high is not None and last_f is not None and last_f >= float(candle_high) * 0.999 and (mom15 or 0) >= 0)
        )
        if trigger_ok and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 78.0
            stop = vwap
            detail = "NU: 15m above VWAP → break candle high"
            reasons += ["15m close ≥ VWAP", "Break of trigger candle high", "Risk: close back below VWAP"]
        elif below:
            action = "WAIT"
            detail = "NU: below VWAP — no long; risk line is VWAP reclaim"
            reasons += ["Below VWAP — stand aside / exit longs"]
        else:
            detail = "NU: waiting for 15m close above VWAP then high break"

    elif sym == "NVDA":
        hl = bool(st.get("higher_low")) or (mom15 is not None and mom15 > 0 and above)
        qqq_ok = qqq_above is True
        if qqq_ok and hl and above and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 82.0
            stop = float(st["last_low"]) if st.get("last_low") is not None else (candle_low or last_f)
            detail = "NVDA: QQQ ≥ VWAP + NVDA higher low"
            reasons += ["QQQ above VWAP", "Higher low / constructive 15m", f"Stop below HL candle ({stop})"]
        else:
            detail = "NVDA: need QQQ ≥ VWAP and higher low"
            if qqq_above is False:
                reasons.append("QQQ still below VWAP — no NVDA long")
            if not hl:
                reasons.append("No higher-low yet")
            if not above:
                reasons.append("NVDA below VWAP")

    elif sym == "CAPR":
        base_ok = bool(st.get("tight_base")) and bool(st.get("break_above_base"))
        vol_ok = bool(st.get("volume_expand")) or (mom15 is not None and mom15 >= 0.2)
        if base_ok and vol_ok and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 76.0
            stop = st.get("base_low") or candle_low
            detail = "CAPR: tight 15m base break + volume"
            reasons += ["Tight base break", "Volume/momentum expand", "Do not average down — stop at base low"]
        else:
            detail = "CAPR: wait for tight base break with volume"
            if st.get("spike_risk"):
                reasons.append("Spike risk — skip chase")
            if not st.get("tight_base"):
                reasons.append("No tight 15m base yet")

    elif sym == "ETON":
        # Pullback holds VWAP then bullish reclaim
        hold_vwap = above or (vwap is not None and last_f is not None and last_f >= vwap * 0.998)
        reclaim = bool(st.get("bullish_reclaim")) or (mom15 is not None and mom15 > 0.08 and hold_vwap)
        if hold_vwap and reclaim and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 74.0
            stop = min(x for x in [vwap, st.get("base_low"), candle_low] if x is not None) if vwap is not None else candle_low
            detail = "ETON: VWAP pullback hold → 15m bullish reclaim"
            reasons += ["Held VWAP on pullback", "15m bullish reclaim", "Stop below VWAP / pullback low"]
        else:
            detail = "ETON: waiting for VWAP-hold pullback + reclaim"

    elif sym == "GOOGL":
        hl = bool(st.get("higher_low")) or bool(st.get("bullish_reclaim")) or (
            mom15 is not None and mom15 > 0 and above
        )
        qqq_ok = qqq_above is True
        if qqq_ok and hl and above and not st.get("spike_risk") and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 80.0
            stop = float(st["last_low"]) if st.get("last_low") is not None else (candle_low or last_f)
            detail = "GOOGL: QQQ ≥ VWAP + higher low / reclaim"
            reasons += ["QQQ above VWAP", "Higher low or VWAP reclaim", "No spike chase"]
        else:
            detail = "GOOGL: need QQQ ≥ VWAP + constructive 15m (no spike)"
            if qqq_above is False:
                reasons.append("QQQ below VWAP — no GOOGL long")
            if st.get("spike_risk"):
                reasons.append("Spike — wait for base")
            if not above:
                reasons.append("GOOGL below VWAP")

    elif sym == "NXPI":
        hl = bool(st.get("higher_low")) or bool(st.get("bullish_reclaim")) or (
            mom15 is not None and mom15 > 0 and above
        )
        qqq_ok = qqq_above is True
        if qqq_ok and hl and above and not st.get("spike_risk") and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 78.0
            stop = float(st["last_low"]) if st.get("last_low") is not None else (candle_low or last_f)
            detail = "NXPI: QQQ ≥ VWAP + semi HL / VWAP reclaim"
            reasons += ["QQQ above VWAP", "Semi higher low or VWAP reclaim", "No spike chase"]
        else:
            detail = "NXPI: need QQQ ≥ VWAP + constructive 15m (no spike)"
            if qqq_above is False:
                reasons.append("QQQ below VWAP — no NXPI long")
            if st.get("spike_risk"):
                reasons.append("Spike — wait for base")
            if not above:
                reasons.append("NXPI below VWAP")

    elif sym == "HTFL":
        near_hod = dist_hod is not None and dist_hod >= -1.0
        consol = bool(st.get("tight_base")) or (near_hod and not st.get("spike_risk"))
        breakout = bool(st.get("break_above_base")) or (near_hod and mom15 is not None and mom15 > 0.1 and not st.get("spike_risk"))
        if near_hod and consol and breakout and not st.get("spike_risk") and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 73.0
            stop = st.get("base_low") or candle_low
            detail = "HTFL: HOD consolidation breakout (no spike chase)"
            reasons += ["Near HOD", "Consolidation then break", "Not an immediate spike"]
        else:
            detail = "HTFL: wait for HOD consolidation break — skip spike"
            if st.get("spike_risk"):
                reasons.append("Spike — do not chase")
            if not near_hod:
                reasons.append("Not near HOD yet")

    else:
        # Generic focus sleeve (incl. TSLA path when structure clear)
        bull = above and (
            bool(st.get("higher_low"))
            or bool(st.get("bullish_reclaim"))
            or bool(st.get("break_above_base"))
            or (mom15 is not None and mom15 >= 0.12)
        )
        bear = below and (
            bool(st.get("lower_high"))
            or bool(st.get("bearish_fail"))
            or bool(st.get("break_below_base"))
            or (mom15 is not None and mom15 <= -0.12)
        )
        if bull and phase in {"prep", "power_hour", "regular"}:
            action = "LONG"
            strength = 70.0 if sym == "TSLA" else 62.0
            stop = candle_low or (st.get("base_low"))
            detail = f"{sym}: above VWAP + bullish 15m → LONG"
            reasons += ["Above VWAP", "Bullish 15m structure"]
        elif bear and phase in {"prep", "power_hour", "regular"}:
            action = "SHORT"
            strength = 70.0 if sym == "TSLA" else 62.0
            stop = candle_high or (st.get("base_high"))
            detail = f"{sym}: below VWAP + bearish 15m → SHORT"
            reasons += ["Below VWAP", "Bearish 15m structure"]
        else:
            action = "WATCH" if phase in {"prep", "power_hour"} else "WAIT"
            detail = f"{sym}: no clear 15m VWAP edge yet"
            if above:
                reasons.append("Above VWAP — waiting for bullish trigger")
            elif below:
                reasons.append("Below VWAP — waiting for bearish trigger")
            else:
                reasons.append("VWAP / 15m structure incomplete")

    # --- Shared filters that were previously missing ---
    # 1) Dead-zone chop at VWAP
    if action in {"LONG", "SHORT"} and vs_vwap is not None and abs(float(vs_vwap)) < 0.08:
        action = "WATCH"
        strength = min(strength, 40.0)
        detail = f"{sym}: VWAP chop zone (|vs VWAP| < 0.08%) — stand aside"
        reasons.append("VWAP dead zone — no edge")

    # 2) Spike veto (generic + megas; HTFL already gated)
    if action in {"LONG", "SHORT"} and st.get("spike_risk") and sym not in {"CAPR"}:
        action = "WATCH"
        strength = min(strength, 42.0)
        detail = f"{sym}: spike bar — wait for base/reclaim (no chase)"
        reasons.append("Spike veto")

    # 3) QQQ breadth for mega-cap longs
    if action == "LONG" and sym in MEGA_BREADTH and qqq_above is False:
        action = "WAIT"
        strength = min(strength, 38.0)
        detail = f"{sym}: blocked — QQQ below VWAP (breadth filter)"
        reasons.append("Mega long needs QQQ ≥ VWAP")

    now_et = now.astimezone(ET) if now and now.tzinfo else (now.replace(tzinfo=ET) if now else datetime.now(ET))
    tnow = now_et.time()

    # 4) Early power-hour chaos (15:00–15:15): demote fresh generic entries
    if (
        action in {"LONG", "SHORT"}
        and phase == "power_hour"
        and POWER_HOUR_START <= tnow < time(15, 15)
        and not special
    ):
        strength = min(strength, 55.0)
        reasons.append("First 15m of power hour — size down / wait for confirm")

    # 5) No new entries after 15:45 ET flatten clock
    if phase == "power_hour" and tnow >= time(15, 45):
        if action in {"LONG", "SHORT"}:
            action = "WAIT"
            strength = 30.0
            detail = f"{sym}: no new entries after 15:45 ET — flatten / manage only"
        reasons.append("Past 15:45 flatten — no new risk")
        if action not in {"LONG", "SHORT"} and not detail:
            detail = f"{sym}: no new entries after 15:45 ET — flatten / manage only"

    # 6) Prep window: keep named playbooks as PREP strength; generics stay WATCH-ish
    if phase == "prep" and action in {"LONG", "SHORT"} and not special:
        strength = min(strength, 52.0)
        reasons.append("Prep only — wait for 15:00 ET to size in")

    # 7) TSLA: require clearer structure; prefer QQQ aligned on longs
    if sym == "TSLA" and action == "LONG" and qqq_above is False:
        action = "WAIT"
        strength = min(strength, 40.0)
        detail = "TSLA: long blocked while QQQ < VWAP"
        reasons.append("TSLA long prefers QQQ ≥ VWAP")
    if sym == "TSLA" and action == "SHORT" and qqq_above is True and vs_vwap is not None and abs(float(vs_vwap)) < 0.35:
        # Weak short against strong QQQ near VWAP
        action = "WATCH"
        strength = min(strength, 45.0)
        reasons.append("TSLA short soft — QQQ still ≥ VWAP")

    if phase == "closed" and action in {"LONG", "SHORT"}:
        reasons.append("After close — treat as plan for next power hour")
        strength = min(strength, 55.0)

    risk_line = rule_risk
    if stop is not None:
        risk_line = f"{rule_risk} · stop ≈ ${float(stop):.2f}"
    if "average" not in risk_line.lower() and "avg" not in risk_line.lower():
        risk_line = f"{risk_line} · never average down"

    headline = f"{action} {sym}"
    if special:
        headline = f"{action} {sym} · playbook"

    sig = PowerHourSignal(
        action=action,
        symbol=sym,
        strength=round(strength, 1),
        headline=headline,
        detail=detail or rule_trigger,
        trigger=rule_trigger,
        risk_line=risk_line,
        playbook=playbook,
        session_phase=phase,
        last=last_f,
        vwap=round(vwap, 4) if vwap is not None else None,
        vs_vwap_pct=round(vs_vwap, 3) if vs_vwap is not None else None,
        qqq_above_vwap=qqq_above,
        day_high=day_high,
        dist_from_hod_pct=round(dist_hod, 3) if dist_hod is not None else None,
        mom_15m_pct=mom15,
        candle_high=float(candle_high) if candle_high is not None else None,
        candle_low=float(candle_low) if candle_low is not None else None,
        stop=float(stop) if stop is not None else None,
        special=bool(special),
        reasons=reasons,
    )
    if action in {"LONG", "SHORT"}:
        ts = signal_timestamps()
        sig.signaled_at = ts["signaled_at"]
        sig.signaled_at_cst = ts["signaled_at_cst"]
    return sig


def resolve_power_hour_symbols(
    symbols: list[str] | str | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> list[str]:
    cfg = config or {}
    raw = symbols
    if raw is None:
        raw = (cfg.get("actions") or {}).get("power_hour_symbols", "focus")
    if isinstance(raw, str) and raw.strip().lower() in {"", "focus", "all", "tickers"}:
        raw = list(cfg.get("tickers") or [])
    if isinstance(raw, str):
        raw = [raw]
    if not raw:
        raw = list(cfg.get("tickers") or SPECIAL_TICKERS)

    seen: set[str] = set()
    out: list[str] = []
    for sym in list(SPECIAL_TICKERS) + [str(s).upper() for s in raw]:
        key = str(sym).replace(".", "-").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def build_power_hour_board(
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    symbols: list[str] | str | None = None,
    config: dict[str, Any] | None = None,
    bars_map: dict[str, pd.DataFrame] | None = None,
    fetch_bars: bool = False,
    max_bar_fetch: int = 16,
    aliases: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    quotes = quotes or {}
    aliases = aliases or {}
    bars_map = dict(bars_map or {})
    symbols = resolve_power_hour_symbols(symbols, config=config)
    phase = session_phase(now)

    # Fetch intraday bars for specials + weakest/strongest tape (capped)
    fetch_list = [s for s in SPECIAL_TICKERS if s in symbols or s == "QQQ"]
    if "QQQ" not in fetch_list:
        fetch_list.insert(0, "QQQ")
    ranked = sorted(
        [s for s in symbols if s not in fetch_list],
        key=lambda s: abs(float((quotes.get(s) or {}).get("mom_15m_pct") or (quotes.get(s) or {}).get("session_change_pct") or 0)),
        reverse=True,
    )
    for s in ranked:
        if len(fetch_list) >= max_bar_fetch:
            break
        fetch_list.append(s)

    if fetch_bars:
        for s in fetch_list:
            if s in bars_map:
                continue
            bars_map[s] = fetch_intraday_1m(s, yahoo_symbol=aliases.get(s))  # type: ignore[assignment]

    qqq_bars = bars_map.get("QQQ")
    qqq_vwap = compute_vwap(qqq_bars) if qqq_bars is not None else _quote_vwap_proxy(quotes.get("QQQ"))

    signals: list[PowerHourSignal] = []
    for sym in symbols:
        sig = decide_power_hour(
            sym,
            quote=quotes.get(sym),
            bars_1m=bars_map.get(sym),
            qqq_quote=quotes.get("QQQ"),
            qqq_vwap=qqq_vwap,
            phase=phase,
            now=now,
        )
        signals.append(sig)

    longs = [s for s in signals if s.action == "LONG"]
    shorts = [s for s in signals if s.action == "SHORT"]
    watches = [s for s in signals if s.action in {"WAIT", "WATCH"}]
    specials = [s for s in signals if s.special]
    longs.sort(key=lambda s: s.strength, reverse=True)
    shorts.sort(key=lambda s: s.strength, reverse=True)

    primary = None
    for pool in (longs, shorts, specials, watches):
        if pool:
            primary = pool[0]
            break

    return {
        "desk": "power_hour",
        "title": "Power Hour — 15m VWAP LONG / SHORT",
        "generated_at": datetime.now(ET).isoformat(),
        "session_phase": phase,
        "qqq_vwap": qqq_vwap,
        "qqq_above_vwap": (
            float((quotes.get("QQQ") or {}).get("last")) >= float(qqq_vwap)
            if qqq_vwap is not None and (quotes.get("QQQ") or {}).get("last") is not None
            else None
        ),
        "symbols": symbols,
        "special_rules": SPECIAL_RULES,
        "primary": primary.to_dict() if primary else None,
        "long": [s.to_dict() for s in longs],
        "short": [s.to_dict() for s in shorts],
        "special": [s.to_dict() for s in specials],
        "watch": [s.to_dict() for s in watches],
        "all": [s.to_dict() for s in signals],
        "counts": {
            "long": len(longs),
            "short": len(shorts),
            "watch": len(watches),
            "special": len(specials),
            "names": len(symbols),
        },
        "playbook": [
            "Window: prep 14:30 ET · power hour 15:00–16:00 ET · no new entries after 15:45 ET.",
            "Shared: skip VWAP chop (|vs VWAP|<0.08%); spike veto; never average down; half-size first 15m of PH on generics.",
            "Mega longs (NVDA/GOOGL/TSLA/AAPL/…): require QQQ ≥ VWAP.",
            "NU: 15m close above VWAP → break that candle high · exit close back below VWAP.",
            "NVDA: QQQ also above VWAP + NVDA higher low · stop below HL candle.",
            "GOOGL: QQQ ≥ VWAP + higher low / VWAP reclaim · no spike chase.",
            "NXPI: semi — QQQ ≥ VWAP + HL / VWAP reclaim · no spike chase · never average down.",
            "CAPR: tight 15m base break with volume · stop below base low · no average down.",
            "ETON: pullback holds VWAP → 15m bullish reclaim · stop below VWAP/pullback low.",
            "HTFL: consolidation near HOD breakout — not a spike chase.",
            "TSLA + focus sleeve: LONG above VWAP with bullish 15m; SHORT below VWAP with bearish 15m.",
        ],
        "disclaimer": (
            "Educational / research only. VWAP from delayed Yahoo 1m bars when fetched; "
            "otherwise day-range mid proxy. Not financial advice."
        ),
    }
