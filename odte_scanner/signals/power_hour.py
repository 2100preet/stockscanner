"""Power Hour desk — confluence LONG / SHORT for the close.

Layers (best-practice desk stack):
  1) 15m VWAP structure (named playbooks + generic sleeve)
  2) Session / market-mover momentum
  3) Ensemble scan score
  4) Bullish / bearish option-flow (Yahoo proxy)
  5) Dealer GEX regime — positive_gamma (MM long gamma → mean-revert /
     VWAP reclaim) vs negative_gamma (MM short gamma → trend continuation)

Named playbooks still apply (NU / NVDA / CAPR / ETON / HTFL / TSLA / GOOGL / NXPI).
Leaders board surfaces high-confluence names (NBIS, CRWV, AVGO, IWM, …)
even when a named playbook stays WAIT.

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

SPECIAL_TICKERS = ("NU", "NVDA", "CAPR", "ETON", "HTFL", "TSLA", "GOOGL", "NXPI")

# Prefer these for quote/bar fan-out + leaders ranking
PRIORITY_TICKERS = (
    "NBIS",
    "CRWV",
    "AVGO",
    "IWM",
    "GOOGL",
    "MU",
    "NVDA",
    "TSLA",
    "AAPL",
    "AMD",
    "SPY",
    "QQQ",
)

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
    session_phase: str = "unknown"
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
    confluence: float | None = None
    flow_score: float | None = None
    ensemble_score: float | None = None
    session_change_pct: float | None = None
    mover_rank: int | None = None
    gex_regime: str | None = None
    gex_bias: str | None = None  # trend | mean_revert | unknown
    mm_note: str | None = None

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

    if len(lows) >= 4:
        out["higher_low"] = float(lows.iloc[-1]) > float(lows.iloc[-3]) and float(closes.iloc[-1]) >= float(
            closes.iloc[-2]
        )
        out["lower_high"] = float(highs.iloc[-1]) < float(highs.iloc[-3]) and float(closes.iloc[-1]) <= float(
            closes.iloc[-2]
        )

    recent = bars_15.iloc[-4:-1] if len(bars_15) >= 4 else bars_15.iloc[:-1]
    if len(recent) >= 2 and "High" in recent.columns:
        base_high = float(recent["High"].max())
        base_low = float(recent["Low"].min())
        out["base_high"] = base_high
        out["base_low"] = base_low
        width = (base_high - base_low) / max(base_low, 1e-6) * 100
        out["tight_base"] = width <= 1.2
        last = float(closes.iloc[-1])
        out["break_above_base"] = last > base_high
        out["break_below_base"] = last < base_low

    if len(vols) >= 4 and float(vols.iloc[-4:-1].mean() or 0) > 0:
        out["volume_expand"] = float(vols.iloc[-1]) >= 1.25 * float(vols.iloc[-4:-1].mean())

    out["bullish_reclaim"] = float(closes.iloc[-1]) > float(closes.iloc[-2]) and float(closes.iloc[-1]) >= float(
        highs.iloc[-2]
    ) * 0.998
    out["bearish_fail"] = float(closes.iloc[-1]) < float(closes.iloc[-2]) and float(closes.iloc[-1]) <= float(
        lows.iloc[-2]
    ) * 1.002

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
    hi, lo = q.get("day_high"), q.get("day_low")
    if hi is not None and lo is not None:
        try:
            return (float(hi) + float(lo)) / 2.0
        except Exception:  # noqa: BLE001
            return None
    return None


def index_flow_by_symbol(option_flow: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in list((option_flow or {}).get("prints") or (option_flow or {}).get("golden") or []):
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        sc = float(p.get("flow_score") or 0)
        cur = out.get(sym)
        if cur is None or abs(sc) > abs(float(cur.get("flow_score") or 0)):
            out[sym] = {
                "flow_score": sc,
                "tier": p.get("tier"),
                "right": p.get("right") or p.get("call_put"),
                "strike": p.get("strike"),
                "expiry": p.get("expiry") or p.get("expiration"),
                "premium_notional": float(p.get("premium_notional") or p.get("premium") or 0),
                "sentiment": p.get("sentiment")
                or ("bullish" if sc > 0 else ("bearish" if sc < 0 else "neutral")),
            }
        else:
            out[sym]["premium_notional"] = float(out[sym].get("premium_notional") or 0) + float(
                p.get("premium_notional") or p.get("premium") or 0
            )
    return out


def index_gex_by_symbol(dealer_edge: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in (dealer_edge or {}).get("profiles") or []:
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        regime = str(p.get("regime") or "")
        # Dealer short-options framing: +GEX ⇒ dealers long gamma (mean-revert);
        # −GEX ⇒ dealers short gamma (trend amplify).
        if "negative" in regime:
            bias = "trend"
            mm = "MM short gamma — trend continuation favored"
        elif "positive" in regime:
            bias = "mean_revert"
            mm = "MM long gamma — mean-revert / VWAP reclaim favored (no chase)"
        else:
            bias = "unknown"
            mm = "GEX regime unknown"
        out[sym] = {
            "regime": regime or None,
            "net_gex": p.get("net_gex"),
            "flip": p.get("flip"),
            "call_wall": p.get("call_wall"),
            "put_wall": p.get("put_wall"),
            "spot": p.get("spot"),
            "gex_bias": bias,
            "mm_note": mm,
        }
    return out


def index_scores_by_symbol(scores: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in scores or []:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        out[sym] = row
    return out


def index_movers(market: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i, row in enumerate((market or {}).get("by_score") or []):
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        out[sym] = {**row, "mover_rank": i}
    return out


def _gex_long_ok(
    *,
    gex: dict[str, Any] | None,
    above: bool,
    below: bool,
    last_f: float | None,
    day_chg: float | None,
) -> tuple[bool, str]:
    """Whether dealer GEX supports a power-hour long."""
    g = gex or {}
    bias = g.get("gex_bias") or "unknown"
    flip = g.get("flip")
    put_wall = g.get("put_wall")
    call_wall = g.get("call_wall")

    if bias == "trend":
        # Negative gamma: favor continuation — need positive momentum
        ok = (day_chg is not None and day_chg >= 0.2) or above
        return ok, "Neg-GEX trend long OK" if ok else "Neg-GEX needs upside momentum"
    if bias == "mean_revert":
        # Positive gamma (MM long): prefer reclaim / support, not HOD chase
        near_put = False
        if last_f is not None and put_wall:
            try:
                near_put = abs(last_f / float(put_wall) - 1.0) * 100 <= 1.25
            except Exception:  # noqa: BLE001
                near_put = False
        above_flip = False
        if last_f is not None and flip is not None:
            try:
                above_flip = last_f >= float(flip) * 0.998
            except Exception:  # noqa: BLE001
                above_flip = False
        under_call = True
        if last_f is not None and call_wall:
            try:
                under_call = last_f <= float(call_wall) * 1.01
            except Exception:  # noqa: BLE001
                under_call = True
        # Soft: above VWAP or reclaiming flip / holding put wall; avoid melt-up chase
        chasing = day_chg is not None and day_chg >= 6.0
        ok = (above or near_put or above_flip) and under_call and not chasing
        return ok, (
            "Pos-GEX mean-revert long OK"
            if ok
            else "Pos-GEX — wait VWAP/flip reclaim (no chase)"
        )
    # Unknown GEX: neutral pass
    return True, "GEX neutral"


def _gex_short_ok(
    *,
    gex: dict[str, Any] | None,
    below: bool,
    day_chg: float | None,
) -> tuple[bool, str]:
    g = gex or {}
    bias = g.get("gex_bias") or "unknown"
    if bias == "trend":
        ok = (day_chg is not None and day_chg <= -0.2) or below
        return ok, "Neg-GEX trend short OK" if ok else "Neg-GEX needs downside momentum"
    if bias == "mean_revert":
        # Fade rips into call wall / failed VWAP
        ok = below or (day_chg is not None and day_chg <= -0.35)
        return ok, "Pos-GEX fade OK" if ok else "Pos-GEX — no short without VWAP fail"
    return True, "GEX neutral"


def compute_confluence(
    symbol: str,
    *,
    above: bool,
    below: bool,
    vs_vwap: float | None,
    mom15: float | None,
    day_chg: float | None,
    st: dict[str, Any],
    score_row: dict[str, Any] | None = None,
    flow_row: dict[str, Any] | None = None,
    gex_row: dict[str, Any] | None = None,
    mover_row: dict[str, Any] | None = None,
    qqq_above: bool | None = None,
    last_f: float | None = None,
) -> dict[str, Any]:
    """Multi-factor confluence score used to promote leaders + fire LONG/SHORT without bars."""
    score_row = score_row or {}
    flow_row = flow_row or {}
    gex_row = gex_row or {}
    mover_row = mover_row or {}

    ensemble = score_row.get("ensemble_score")
    try:
        ensemble_f = float(ensemble) if ensemble is not None else None
    except Exception:  # noqa: BLE001
        ensemble_f = None
    flow_sc = flow_row.get("flow_score")
    try:
        flow_f = float(flow_sc) if flow_sc is not None else None
    except Exception:  # noqa: BLE001
        flow_f = None
    mover_rank = mover_row.get("mover_rank")
    try:
        mover_i = int(mover_rank) if mover_rank is not None else None
    except Exception:  # noqa: BLE001
        mover_i = None

    long_votes = 0.0
    short_votes = 0.0
    notes: list[str] = []

    # 1) VWAP / structure
    if above:
        long_votes += 1.25
        notes.append("Above VWAP")
    elif below:
        short_votes += 1.25
        notes.append("Below VWAP")
    if st.get("higher_low") or st.get("bullish_reclaim") or st.get("break_above_base"):
        long_votes += 1.0
        notes.append("Bullish 15m structure")
    if st.get("lower_high") or st.get("bearish_fail") or st.get("break_below_base"):
        short_votes += 1.0
        notes.append("Bearish 15m structure")

    # 2) Momentum / movers
    if mom15 is not None:
        if mom15 >= 0.08:
            long_votes += 1.0
            notes.append(f"15m mom {mom15:.2f}%")
        elif mom15 <= -0.08:
            short_votes += 1.0
            notes.append(f"15m mom {mom15:.2f}%")
    if day_chg is not None:
        if day_chg >= 0.35:
            long_votes += 1.1 if (mover_i is not None and mover_i < 25) else 0.85
            notes.append(f"Day {day_chg:+.2f}%")
        elif day_chg <= -0.35:
            short_votes += 1.1 if (mover_i is not None and mover_i < 25) else 0.85
            notes.append(f"Day {day_chg:+.2f}%")
    if mover_i is not None and mover_i < 15:
        notes.append(f"Mover #{mover_i + 1}")
        if day_chg is not None and day_chg > 0:
            long_votes += 0.5
        elif day_chg is not None and day_chg < 0:
            short_votes += 0.5

    # 3) Ensemble score
    if ensemble_f is not None:
        if ensemble_f >= 68 and score_row.get("bullish") is not False:
            long_votes += 1.2
            notes.append(f"Ensemble {ensemble_f:.0f}")
        elif ensemble_f >= 60 and score_row.get("bullish"):
            long_votes += 0.8
            notes.append(f"Ensemble {ensemble_f:.0f}")
        elif ensemble_f < 48 and score_row.get("bullish") is False:
            short_votes += 0.7
            notes.append(f"Weak ensemble {ensemble_f:.0f}")

    # 4) Option flow
    if flow_f is not None:
        if flow_f >= 35:
            long_votes += 1.35 if flow_f >= 70 else 1.0
            notes.append(f"Bullish flow {flow_f:.0f}")
        elif flow_f <= -35:
            short_votes += 1.35 if flow_f <= -70 else 1.0
            notes.append(f"Bearish flow {flow_f:.0f}")

    # 5) Dealer GEX / MM gamma
    gex_l_ok, gex_l_note = _gex_long_ok(
        gex=gex_row, above=above, below=below, last_f=last_f, day_chg=day_chg
    )
    gex_s_ok, gex_s_note = _gex_short_ok(gex=gex_row, below=below, day_chg=day_chg)
    if gex_row.get("regime"):
        notes.append(gex_row.get("mm_note") or gex_l_note)
        if gex_row.get("gex_bias") == "trend" and day_chg is not None and day_chg > 0:
            long_votes += 0.6
        if gex_row.get("gex_bias") == "mean_revert" and above:
            long_votes += 0.45
        if gex_row.get("gex_bias") == "trend" and day_chg is not None and day_chg < 0:
            short_votes += 0.6

    # Mega breadth soft penalty (hard block applied later)
    if symbol in MEGA_BREADTH and qqq_above is False:
        long_votes -= 1.5
        notes.append("QQQ < VWAP — mega long soft-block")

    # Priority sleeve bonus (desk focus names)
    if symbol in PRIORITY_TICKERS and long_votes >= short_votes:
        long_votes += 0.25
    if symbol in PRIORITY_TICKERS and short_votes > long_votes:
        short_votes += 0.25

    confluence = round(max(long_votes, short_votes) * 12.5, 1)  # ~ scale to 0–100-ish
    bias = "long" if long_votes > short_votes + 0.35 else ("short" if short_votes > long_votes + 0.35 else "neutral")

    return {
        "long_votes": round(long_votes, 2),
        "short_votes": round(short_votes, 2),
        "confluence": confluence,
        "bias": bias,
        "notes": notes,
        "ensemble_score": ensemble_f,
        "flow_score": flow_f,
        "mover_rank": mover_i,
        "gex_regime": gex_row.get("regime"),
        "gex_bias": gex_row.get("gex_bias"),
        "mm_note": gex_row.get("mm_note"),
        "gex_long_ok": gex_l_ok,
        "gex_short_ok": gex_s_ok,
        "gex_long_note": gex_l_note,
        "gex_short_note": gex_s_note,
        "day_chg": day_chg,
    }


def decide_power_hour(
    symbol: str,
    *,
    quote: dict[str, Any] | None = None,
    bars_1m: pd.DataFrame | None = None,
    qqq_quote: dict[str, Any] | None = None,
    qqq_vwap: float | None = None,
    phase: str | None = None,
    now: datetime | None = None,
    score_row: dict[str, Any] | None = None,
    flow_row: dict[str, Any] | None = None,
    gex_row: dict[str, Any] | None = None,
    mover_row: dict[str, Any] | None = None,
) -> PowerHourSignal:
    sym = str(symbol).upper()
    q = quote or {}
    phase = phase or session_phase(now)
    special = SPECIAL_RULES.get(sym)
    playbook = sym if special else "generic"
    rule_trigger = (special or {}).get("trigger") or (
        "Confluence LONG/SHORT: VWAP structure + mover momentum + score + flow + dealer GEX"
    )
    rule_risk = (special or {}).get("risk") or "Stop beyond the signal 15m candle; no averaging into losers"

    last = q.get("last")
    if last is None:
        last = q.get("live_last")
    last_f = float(last) if last is not None else None
    day_high = float(q["day_high"]) if q.get("day_high") is not None else None
    mom15 = float(q["mom_15m_pct"]) if q.get("mom_15m_pct") is not None else None
    day_chg = None
    for k in ("session_change_pct", "change_pct"):
        if q.get(k) is not None:
            try:
                day_chg = float(q[k])
                break
            except Exception:  # noqa: BLE001
                pass
    if day_chg is None and mover_row and mover_row.get("change_pct") is not None:
        try:
            day_chg = float(mover_row["change_pct"])
        except Exception:  # noqa: BLE001
            day_chg = None

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
        trigger_ok = above and (
            (st.get("break_above_base") and st.get("prev_close") is not None and vwap is not None and float(st["prev_close"]) >= vwap)
            or (mom15 is not None and mom15 > 0.05 and above and st.get("bullish_reclaim"))
            or (above and candle_high is not None and last_f is not None and last_f >= float(candle_high) * 0.999 and (mom15 or 0) >= 0)
        )
        if trigger_ok and phase in {"prep", "power_hour", "regular", "closed"}:
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
        if qqq_ok and hl and above and phase in {"prep", "power_hour", "regular", "closed"}:
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
        if base_ok and vol_ok and phase in {"prep", "power_hour", "regular", "closed"}:
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
        hold_vwap = above or (vwap is not None and last_f is not None and last_f >= vwap * 0.998)
        reclaim = bool(st.get("bullish_reclaim")) or (mom15 is not None and mom15 > 0.08 and hold_vwap)
        if hold_vwap and reclaim and phase in {"prep", "power_hour", "regular", "closed"}:
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
        if qqq_ok and hl and above and not st.get("spike_risk") and phase in {"prep", "power_hour", "regular", "closed"}:
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
        if qqq_ok and hl and above and not st.get("spike_risk") and phase in {"prep", "power_hour", "regular", "closed"}:
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
        breakout = bool(st.get("break_above_base")) or (
            near_hod and mom15 is not None and mom15 > 0.1 and not st.get("spike_risk")
        )
        if near_hod and consol and breakout and not st.get("spike_risk") and phase in {
            "prep",
            "power_hour",
            "regular",
            "closed",
        }:
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
        # Generic sleeve (structure path)
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
        if bull and phase in {"prep", "power_hour", "regular", "closed"}:
            action = "LONG"
            strength = 70.0 if sym == "TSLA" else 62.0
            stop = candle_low or (st.get("base_low"))
            detail = f"{sym}: above VWAP + bullish 15m → LONG"
            reasons += ["Above VWAP", "Bullish 15m structure"]
        elif bear and phase in {"prep", "power_hour", "regular", "closed"}:
            action = "SHORT"
            strength = 70.0 if sym == "TSLA" else 62.0
            stop = candle_high or (st.get("base_high"))
            detail = f"{sym}: below VWAP + bearish 15m → SHORT"
            reasons += ["Below VWAP", "Bearish 15m structure"]
        else:
            action = "WATCH" if phase in {"prep", "power_hour", "closed"} else "WAIT"
            detail = f"{sym}: no clear 15m VWAP edge yet"
            if above:
                reasons.append("Above VWAP — waiting for bullish trigger")
            elif below:
                reasons.append("Below VWAP — waiting for bearish trigger")
            else:
                reasons.append("VWAP / 15m structure incomplete")

    # --- Confluence overlay (fires when structure incomplete / boosts leaders) ---
    conf = compute_confluence(
        sym,
        above=above,
        below=below,
        vs_vwap=vs_vwap,
        mom15=mom15,
        day_chg=day_chg,
        st=st,
        score_row=score_row,
        flow_row=flow_row,
        gex_row=gex_row,
        mover_row=mover_row,
        qqq_above=qqq_above,
        last_f=last_f,
    )
    long_votes = float(conf["long_votes"])
    short_votes = float(conf["short_votes"])

    if action not in {"LONG", "SHORT"} and last_f is not None and phase in {
        "prep",
        "power_hour",
        "regular",
        "closed",
    }:
        # Need ≥2.2 confluence votes + GEX alignment to promote
        if (
            conf["bias"] == "long"
            and long_votes >= 2.2
            and conf.get("gex_long_ok", True)
            and not (sym in MEGA_BREADTH and qqq_above is False)
        ):
            action = "LONG"
            strength = min(88.0, 52.0 + long_votes * 8.0)
            playbook = "confluence" if not special else playbook
            stop = stop or candle_low or (vwap if above else None)
            detail = f"{sym}: confluence LONG (VWAP/mover/score/flow/GEX)"
            reasons += [f"Confluence long votes {long_votes:.1f}"] + list(conf["notes"][:4])
            if conf.get("gex_long_note"):
                reasons.append(str(conf["gex_long_note"]))
        elif conf["bias"] == "short" and short_votes >= 2.2 and conf.get("gex_short_ok", True):
            action = "SHORT"
            strength = min(86.0, 52.0 + short_votes * 8.0)
            playbook = "confluence" if not special else playbook
            stop = stop or candle_high or (vwap if below else None)
            detail = f"{sym}: confluence SHORT (VWAP/mover/score/flow/GEX)"
            reasons += [f"Confluence short votes {short_votes:.1f}"] + list(conf["notes"][:4])
            if conf.get("gex_short_note"):
                reasons.append(str(conf["gex_short_note"]))
        elif long_votes >= 1.4 or short_votes >= 1.4:
            action = "WATCH"
            strength = max(strength, 40.0 + max(long_votes, short_votes) * 5.0)
            if not detail or "no clear" in detail.lower() or "need" in detail.lower() or "waiting" in detail.lower():
                detail = f"{sym}: confluence building ({conf['bias']}) — not triggered yet"
            reasons += list(conf["notes"][:3])

    if action in {"LONG", "SHORT"}:
        # Boost strength when confluence agrees with structure playbook
        if action == "LONG" and long_votes >= 2.0:
            strength = min(92.0, strength + long_votes * 2.5)
        if action == "SHORT" and short_votes >= 2.0:
            strength = min(90.0, strength + short_votes * 2.5)

    # --- Shared filters ---
    if action in {"LONG", "SHORT"} and vs_vwap is not None and abs(float(vs_vwap)) < 0.08:
        action = "WATCH"
        strength = min(strength, 40.0)
        detail = f"{sym}: VWAP chop zone (|vs VWAP| < 0.08%) — stand aside"
        reasons.append("VWAP dead zone — no edge")

    if action in {"LONG", "SHORT"} and st.get("spike_risk") and sym not in {"CAPR"}:
        action = "WATCH"
        strength = min(strength, 42.0)
        detail = f"{sym}: spike bar — wait for base/reclaim (no chase)"
        reasons.append("Spike veto")

    if action == "LONG" and sym in MEGA_BREADTH and qqq_above is False:
        action = "WAIT"
        strength = min(strength, 38.0)
        detail = f"{sym}: blocked — QQQ below VWAP (breadth filter)"
        reasons.append("Mega long needs QQQ ≥ VWAP")

    now_et = now.astimezone(ET) if now and now.tzinfo else (now.replace(tzinfo=ET) if now else datetime.now(ET))
    tnow = now_et.time()

    if (
        action in {"LONG", "SHORT"}
        and phase == "power_hour"
        and POWER_HOUR_START <= tnow < time(15, 15)
        and not special
        and playbook == "confluence"
    ):
        strength = min(strength, 58.0)
        reasons.append("First 15m of power hour — size down / wait for confirm")

    if phase == "power_hour" and tnow >= time(15, 45):
        if action in {"LONG", "SHORT"}:
            action = "WAIT"
            strength = 30.0
            detail = f"{sym}: no new entries after 15:45 ET — flatten / manage only"
        reasons.append("Past 15:45 flatten — no new risk")
        if action not in {"LONG", "SHORT"} and not detail:
            detail = f"{sym}: no new entries after 15:45 ET — flatten / manage only"

    if phase == "prep" and action in {"LONG", "SHORT"} and not special:
        strength = min(strength, 55.0)
        reasons.append("Prep only — wait for 15:00 ET to size in")

    if sym == "TSLA" and action == "LONG" and qqq_above is False:
        action = "WAIT"
        strength = min(strength, 40.0)
        detail = "TSLA: long blocked while QQQ < VWAP"
        reasons.append("TSLA long prefers QQQ ≥ VWAP")
    if sym == "TSLA" and action == "SHORT" and qqq_above is True and vs_vwap is not None and abs(float(vs_vwap)) < 0.35:
        action = "WATCH"
        strength = min(strength, 45.0)
        reasons.append("TSLA short soft — QQQ still ≥ VWAP")

    if phase == "closed" and action in {"LONG", "SHORT"}:
        reasons.append("After close — treat as plan for next power hour")
        strength = min(strength, 62.0)

    risk_line = rule_risk
    if stop is not None:
        risk_line = f"{rule_risk} · stop ≈ ${float(stop):.2f}"
    if "average" not in risk_line.lower() and "avg" not in risk_line.lower():
        risk_line = f"{risk_line} · never average down"

    headline = f"{action} {sym}"
    if special:
        headline = f"{action} {sym} · playbook"
    elif playbook == "confluence":
        headline = f"{action} {sym} · confluence"

    # Trigger text: prefer confluence summary when that path fired
    trigger_out = rule_trigger
    if playbook == "confluence":
        trigger_out = (
            "Confluence: VWAP/structure + mover momentum + ensemble + option flow + dealer GEX"
        )

    sig = PowerHourSignal(
        action=action,
        symbol=sym,
        strength=round(strength, 1),
        headline=headline,
        detail=detail or rule_trigger,
        trigger=trigger_out,
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
        confluence=float(conf.get("confluence") or 0),
        flow_score=conf.get("flow_score"),
        ensemble_score=conf.get("ensemble_score"),
        session_change_pct=day_chg,
        mover_rank=conf.get("mover_rank"),
        gex_regime=conf.get("gex_regime"),
        gex_bias=conf.get("gex_bias"),
        mm_note=conf.get("mm_note"),
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
    for sym in list(SPECIAL_TICKERS) + list(PRIORITY_TICKERS) + [str(s).upper() for s in raw]:
        key = str(sym).replace(".", "-").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def seed_quotes_from_scan(
    quotes: dict[str, dict[str, Any]] | None,
    *,
    scores: list[dict[str, Any]] | None = None,
    market: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fill missing Power Hour quotes from scan scores / market movers (Pages offline)."""
    out: dict[str, dict[str, Any]] = {str(k).upper(): dict(v) for k, v in (quotes or {}).items() if isinstance(v, dict)}

    def _ensure(sym: str, patch: dict[str, Any]) -> None:
        key = str(sym).upper()
        if not key:
            return
        cur = out.get(key) or {}
        merged = dict(cur)
        for k, v in patch.items():
            if v is None:
                continue
            if merged.get(k) is None:
                merged[k] = v
        if merged.get("last") is None and merged.get("live_last") is not None:
            merged["last"] = merged["live_last"]
        if merged:
            out[key] = merged

    for row in scores or []:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        last = row.get("last_price")
        if last is None:
            last = row.get("entry")
        _ensure(
            sym,
            {
                "last": float(last) if last is not None else None,
                "live_last": float(last) if last is not None else None,
                "session_change_pct": row.get("session_change_pct") or row.get("change_pct"),
            },
        )

    market = market or {}
    for key in ("by_score", "by_volume", "by_earnings"):
        for row in market.get(key) or []:
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            last = row.get("last")
            _ensure(
                sym,
                {
                    "last": float(last) if last is not None else None,
                    "live_last": float(last) if last is not None else None,
                    "session_change_pct": row.get("change_pct") or row.get("session_change_pct"),
                    "day_volume": row.get("day_volume"),
                },
            )
    return out


def top_closing_bell_bullish(
    *,
    option_flow: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    n: int = 2,
    mover_top_n: int = 25,
) -> dict[str, Any]:
    """Top N tickers with bullish option flow among leading market movers (closing-bell desk)."""
    flow = option_flow or {}
    prints = list(flow.get("prints") or flow.get("golden") or [])
    bullish = [
        p
        for p in prints
        if float(p.get("flow_score") or 0) > 0
        and str(p.get("sentiment") or "").lower() in {"", "bullish"}
    ]
    movers = list((market or {}).get("by_score") or [])[: max(1, int(mover_top_n))]
    mover_rank = {str(r.get("symbol") or "").upper(): i for i, r in enumerate(movers) if r.get("symbol")}
    mover_meta = {str(r.get("symbol") or "").upper(): r for r in movers if r.get("symbol")}

    by_sym: dict[str, dict[str, Any]] = {}
    for p in bullish:
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        sc = float(p.get("flow_score") or 0)
        prem = float(p.get("premium_notional") or p.get("premium") or 0)
        cur = by_sym.get(sym)
        if cur is None or sc > float(cur.get("flow_score") or 0):
            mm = mover_meta.get(sym) or {}
            by_sym[sym] = {
                "symbol": sym,
                "flow_score": sc,
                "premium_notional": prem,
                "tier": p.get("tier"),
                "right": p.get("right") or p.get("call_put") or "C",
                "strike": p.get("strike"),
                "expiry": p.get("expiry") or p.get("expiration"),
                "spot": p.get("spot") or mm.get("last"),
                "change_pct": mm.get("change_pct") or mm.get("session_change_pct"),
                "mover_rank": mover_rank.get(sym),
                "in_movers": sym in mover_rank,
                "sentiment": "bullish",
                "detail": (
                    f"Bullish {p.get('tier') or 'flow'} · "
                    f"{'call' if str(p.get('right') or 'C').upper().startswith('C') else 'put'} "
                    f"${p.get('strike')} exp {p.get('expiry') or p.get('expiration')}"
                ),
            }
        else:
            by_sym[sym]["premium_notional"] = float(by_sym[sym].get("premium_notional") or 0) + prem

    ranked = sorted(
        by_sym.values(),
        key=lambda r: (
            0 if r.get("in_movers") else 1,
            -float(r.get("flow_score") or 0),
            -float(r.get("premium_notional") or 0),
            int(r.get("mover_rank") if r.get("mover_rank") is not None else 10_000),
        ),
    )
    top = ranked[: max(1, int(n))]
    return {
        "title": "Closing bell — top bullish flow (market movers)",
        "n": len(top),
        "rows": top,
        "mover_top_n": int(mover_top_n),
        "note": (
            f"Among top {int(mover_top_n)} market movers, ranked by bullish option-flow score then premium. "
            "Yahoo chain proxy — not OPRA tape."
        ),
    }


def _pick_primary(
    longs: list[PowerHourSignal],
    shorts: list[PowerHourSignal],
    specials: list[PowerHourSignal],
    watches: list[PowerHourSignal],
    leaders: list[PowerHourSignal] | None = None,
) -> PowerHourSignal | None:
    def _scored(pool: list[PowerHourSignal]) -> list[PowerHourSignal]:
        return sorted(
            pool,
            key=lambda s: (
                0 if s.last is not None else 1,
                0 if s.action in {"LONG", "SHORT"} else 1,
                -float(s.confluence or 0),
                -float(s.strength or 0),
                0 if s.special else 1,
                s.symbol or "",
            ),
        )

    for pool in (longs, shorts, leaders or [], specials, watches):
        ranked = [s for s in _scored(pool) if s.last is not None] or _scored(pool)
        if ranked and ranked[0].last is not None:
            return ranked[0]
    return None


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
    scores: list[dict[str, Any]] | None = None,
    market: dict[str, Any] | None = None,
    option_flow: dict[str, Any] | None = None,
    dealer_edge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quotes = seed_quotes_from_scan(quotes, scores=scores, market=market)
    aliases = aliases or {}
    bars_map = dict(bars_map or {})
    symbols = resolve_power_hour_symbols(symbols, config=config)
    phase = session_phase(now)

    score_ix = index_scores_by_symbol(scores)
    flow_ix = index_flow_by_symbol(option_flow)
    gex_ix = index_gex_by_symbol(dealer_edge)
    mover_ix = index_movers(market)

    # Bar fetch priority: QQQ → specials → priority desk names → top |day change| / flow
    fetch_list: list[str] = []
    for s in ("QQQ", *SPECIAL_TICKERS, *PRIORITY_TICKERS):
        if s not in fetch_list and (s in symbols or s == "QQQ"):
            fetch_list.append(s)

    def _priority_key(s: str) -> float:
        q = quotes.get(s) or {}
        chg = abs(float(q.get("mom_15m_pct") or q.get("session_change_pct") or 0))
        fl = abs(float((flow_ix.get(s) or {}).get("flow_score") or 0))
        mr = mover_ix.get(s) or {}
        mover_boost = 20.0 - float(mr.get("mover_rank") or 20) if s in mover_ix else 0.0
        return chg + fl * 0.05 + mover_boost

    ranked = sorted([s for s in symbols if s not in fetch_list], key=_priority_key, reverse=True)
    for s in ranked:
        if len(fetch_list) >= max_bar_fetch:
            break
        fetch_list.append(s)

    bars_fetched = 0
    if fetch_bars:
        for s in fetch_list:
            if s in bars_map and bars_map[s] is not None:
                continue
            df = fetch_intraday_1m(s, yahoo_symbol=aliases.get(s))
            bars_map[s] = df  # type: ignore[assignment]
            if df is not None and not getattr(df, "empty", True):
                bars_fetched += 1

    qqq_bars = bars_map.get("QQQ")
    qqq_vwap = compute_vwap(qqq_bars) if qqq_bars is not None else _quote_vwap_proxy(quotes.get("QQQ"))

    signals: list[PowerHourSignal] = []
    for sym in symbols:
        # Merge mover change into quote if missing
        q = dict(quotes.get(sym) or {})
        mr = mover_ix.get(sym) or {}
        if q.get("session_change_pct") is None and mr.get("change_pct") is not None:
            q["session_change_pct"] = mr.get("change_pct")
        if q.get("last") is None and mr.get("last") is not None:
            q["last"] = mr.get("last")
        sig = decide_power_hour(
            sym,
            quote=q,
            bars_1m=bars_map.get(sym),
            qqq_quote=quotes.get("QQQ"),
            qqq_vwap=qqq_vwap,
            phase=phase,
            now=now,
            score_row=score_ix.get(sym),
            flow_row=flow_ix.get(sym),
            gex_row=gex_ix.get(sym),
            mover_row=mr,
        )
        signals.append(sig)

    longs = [s for s in signals if s.action == "LONG"]
    shorts = [s for s in signals if s.action == "SHORT"]
    watches = [s for s in signals if s.action in {"WAIT", "WATCH"}]
    specials = [s for s in signals if s.special]
    longs.sort(key=lambda s: (float(s.confluence or 0), s.strength), reverse=True)
    shorts.sort(key=lambda s: (float(s.confluence or 0), s.strength), reverse=True)
    specials = sorted(
        specials,
        key=lambda s: (0 if s.last is not None else 1, -float(s.confluence or 0), -float(s.strength or 0)),
    )

    # Leaders: high-confluence names the desk should see (includes WATCH)
    leaders = sorted(
        [s for s in signals if s.last is not None and float(s.confluence or 0) >= 18],
        key=lambda s: (
            0 if s.action in {"LONG", "SHORT"} else 1,
            -float(s.confluence or 0),
            -float(s.strength or 0),
            0 if s.symbol in PRIORITY_TICKERS else 1,
        ),
    )[:16]
    # Always surface priority symbols that have tape even if confluence soft
    have = {s.symbol for s in leaders}
    for s in signals:
        if s.symbol in PRIORITY_TICKERS and s.last is not None and s.symbol not in have:
            leaders.append(s)
            have.add(s.symbol)
    leaders = leaders[:20]

    primary = _pick_primary(longs, shorts, specials, watches, leaders)
    with_last = sum(1 for s in signals if s.last is not None)
    with_vwap = sum(1 for s in signals if s.vwap is not None)
    tape_ok = with_last > 0
    closing = top_closing_bell_bullish(option_flow=option_flow, market=market, n=2)

    return {
        "desk": "power_hour",
        "title": "Power Hour — confluence LONG / SHORT (VWAP · flow · GEX)",
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
        "leaders": [s.to_dict() for s in leaders],
        "watch": [s.to_dict() for s in watches],
        "all": [s.to_dict() for s in signals],
        "closing_bell_bullish": closing,
        "data_quality": {
            "quotes_with_last": with_last,
            "quotes_with_vwap": with_vwap,
            "bars_fetched": bars_fetched,
            "fetch_bars": bool(fetch_bars),
            "gex_profiles": len(gex_ix),
            "flow_symbols": len(flow_ix),
            "tape_ok": tape_ok,
            "note": (
                None
                if tape_ok
                else "No underlying tape for Power Hour — quotes/bars missing. LONG/SHORT stay WAIT."
            ),
        },
        "counts": {
            "long": len(longs),
            "short": len(shorts),
            "watch": len(watches),
            "special": len(specials),
            "leaders": len(leaders),
            "names": len(symbols),
            "with_last": with_last,
            "with_vwap": with_vwap,
        },
        "playbook": [
            "Confluence stack: 15m VWAP structure + mover momentum + ensemble score + option flow + dealer GEX.",
            "Positive GEX (MM long gamma): mean-revert / VWAP-reclaim longs — do not chase melt-ups.",
            "Negative GEX (MM short gamma): favor trend continuation with momentum.",
            "Leaders board lists high-confluence names (NBIS/CRWV/AVGO/IWM/GOOGL/…) even when WATCH.",
            "Window: prep 14:30 ET · power hour 15:00–16:00 ET · no new entries after 15:45 ET.",
            "Mega longs (NVDA/GOOGL/TSLA/AVGO/MU/…): require QQQ ≥ VWAP.",
            "Named playbooks still apply for NU / NVDA / CAPR / ETON / HTFL / GOOGL / NXPI / TSLA.",
        ],
        "disclaimer": (
            "Educational / research only. VWAP from delayed Yahoo 1m bars when fetched; "
            "GEX/flow are Yahoo OI/chain proxies — not live dealer tape. Not financial advice."
        ),
    }
