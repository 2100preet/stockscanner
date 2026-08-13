"""VolSignals-inspired Red Flag proxy (0DTE dealer / charm framework).

VS3D uses proprietary actual market-maker positioning. Signal Desk approximates
the *Red Flag* idea from public 0DTE option chains (Yahoo):

- Customers hedge upside by buying calls → dealers often short those calls.
- Heavy short-gamma / short-call positioning above spot tends to cap rallies.
- Near 0DTE expiry, charm (delta decay) pushes passive dealer hedging — often
  nudging price back toward equilibrium / pin strikes into the close.

This is a **proxy**, not VS3D. Use for risk framing and 0DTE long-call gates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)

RedFlagState = Literal["RED_FLAG", "NEUTRAL", "SUPPORTIVE"]

ET = ZoneInfo("America/New_York")

VOLSIGNALS_NOTE = (
    "Inspired by VolSignals VS3D Red Flag: customer call-buying / dealer short "
    "positioning above spot + 0DTE charm decay can cap rallies into resistance. "
    "Proxy from public chain data — not actual MM positioning."
)

BOTTOM_LINE_RULES = [
    {
        "key": "frmi",
        "ticker": "FRMI",
        "when": "Aug 13 BMO",
        "text": (
            "If you want maximum possible post-earnings upside, watch FRMI tomorrow "
            "morning — but trade the confirmed reaction, not the report blindly."
        ),
    },
    {
        "key": "tssi",
        "ticker": "TSSI",
        "when": "Aug 13 AMC",
        "text": (
            "If you want a more earnings-driven data-center infrastructure trade, use "
            "TSSI tomorrow after close, with a strict rule: only take it if the market "
            "accepts the report through the after-hours high/VWAP."
        ),
    },
    {
        "key": "iren",
        "ticker": "IREN",
        "when": "Aug 27 AMC",
        "text": (
            "If you want the best liquid swing into late August, keep IREN on watch for "
            "Aug. 27; it is the most comparable business theme to NBIS/CRWV, but only "
            "take it if the print confirms AI ARR / contracted revenue ramp — not just "
            "a sympathy bounce."
        ),
    },
]


def _minutes_to_close_et(now: datetime | None = None) -> float | None:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return None
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= close:
        return 0.0
    open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < open_:
        return (close - open_).total_seconds() / 60.0
    return (close - now).total_seconds() / 60.0


def _nearest_expiry(ticker: yf.Ticker) -> str | None:
    try:
        exps = list(ticker.options or [])
    except Exception:  # noqa: BLE001
        return None
    if not exps:
        return None
    return sorted(exps)[0]


def _fetch_calls(symbol: str, *, yahoo_symbol: str | None = None) -> tuple[str | None, Any, float | None]:
    sym = yahoo_symbol or symbol
    t = yf.Ticker(sym)
    exp = _nearest_expiry(t)
    if not exp:
        return None, None, None
    try:
        chain = t.option_chain(exp)
        calls = chain.calls
    except Exception as exc:  # noqa: BLE001
        logger.debug("red_flag chain failed %s: %s", symbol, exc)
        return exp, None, None
    spot = None
    try:
        spot = float(t.fast_info.last_price)
    except Exception:  # noqa: BLE001
        pass
    return exp, calls, spot


def analyze_red_flag(
    symbol: str = "SPY",
    *,
    yahoo_symbol: str | None = None,
    otm_min_pct: float = 0.15,
    otm_max_pct: float = 2.5,
    min_oi: int = 500,
) -> dict[str, Any]:
    """Estimate Red Flag state from 0DTE/near-0DTE call positioning above spot."""
    exp, calls, spot = _fetch_calls(symbol, yahoo_symbol=yahoo_symbol)
    reasons: list[str] = []
    resistance_strikes: list[dict[str, Any]] = []

    if calls is None or calls.empty or spot is None or spot <= 0:
        return {
            "symbol": symbol,
            "state": "NEUTRAL",
            "score": 50.0,
            "expiry": exp,
            "spot": spot,
            "reasons": ["Insufficient option chain for Red Flag proxy"],
            "resistance_strikes": [],
            "charm_pressure": "unknown",
            "block_0dte_long_calls": False,
            "strategy_hint": _strategy_hint("NEUTRAL"),
            "volsignals_note": VOLSIGNALS_NOTE,
            "bottom_line_rules": BOTTOM_LINE_RULES,
            "proxy": True,
        }

    df = calls.copy()
    df["strike"] = df["strike"].astype(float)
    df["openInterest"] = df.get("openInterest", 0).fillna(0).astype(int)
    df["volume"] = df.get("volume", 0).fillna(0).astype(int)
    df["moneyness_pct"] = (df["strike"] - spot) / spot * 100.0

    otm = df[(df["moneyness_pct"] >= otm_min_pct) & (df["moneyness_pct"] <= otm_max_pct)]
    if otm.empty:
        otm = df[df["strike"] > spot]

    # Customer call-buy / dealer short proxy: OTM call OI + volume concentration
    otm = otm.sort_values("openInterest", ascending=False)
    top = otm.head(8)
    total_oi = float(df["openInterest"].sum()) or 1.0
    otm_oi = float(otm["openInterest"].sum())
    otm_vol = float(otm["volume"].sum())
    atm_oi = float(df[df["moneyness_pct"].abs() <= 0.35]["openInterest"].sum())

    otm_share = otm_oi / total_oi
    vol_share = otm_vol / max(float(df["volume"].sum()), 1.0)

    for _, row in top.head(5).iterrows():
        resistance_strikes.append(
            {
                "strike": float(row["strike"]),
                "moneyness_pct": round(float(row["moneyness_pct"]), 2),
                "open_interest": int(row["openInterest"]),
                "volume": int(row["volume"]),
            }
        )

    score = 50.0
    if otm_share >= 0.45:
        score += 18
        reasons.append(f"Heavy OTM call OI above spot ({otm_share:.0%} of chain OI)")
    elif otm_share >= 0.32:
        score += 10
        reasons.append(f"Elevated OTM call OI above spot ({otm_share:.0%})")

    if vol_share >= 0.55 and otm_vol >= 1000:
        score += 12
        reasons.append("Call volume skewed to upside hedges (OTM call buying)")
    elif vol_share >= 0.4:
        score += 6
        reasons.append("Moderate OTM call volume — watch for failed rips into strikes")

    # Nearest major resistance = top OTM strike with meaningful OI
    pin = None
    for row in resistance_strikes:
        if row["open_interest"] >= min_oi:
            pin = row["strike"]
            break
    if pin:
        dist = (pin - spot) / spot * 100
        reasons.append(f"Key call-wall / equilibrium target ≈ ${pin:g} ({dist:+.2f}% above spot)")

    # Charm pressure increases into the 0DTE close (VolSignals framework)
    mins_left = _minutes_to_close_et()
    charm = "off_session"
    if mins_left is not None:
        if mins_left <= 90:
            charm = "high"
            score += 14
            reasons.append(
                "High charm window (<90m to close): 0DTE delta decay → passive dealer "
                "hedging often caps late-day rips"
            )
        elif mins_left <= 180:
            charm = "moderate"
            score += 8
            reasons.append("Moderate charm pressure into 0DTE close")
        else:
            charm = "low"

    # Put-side support below (supportive when puts dominate below spot)
    below = df[df["strike"] < spot * 0.998]
    below_oi = float(below["openInterest"].sum()) if not below.empty else 0.0
    if below_oi > otm_oi * 1.4 and atm_oi > otm_oi * 0.5:
        score -= 12
        reasons.append("Put OI below spot dominates — mild supportive / range bias")

    score = max(0.0, min(100.0, score))

    if score >= 68:
        state: RedFlagState = "RED_FLAG"
    elif score <= 42:
        state = "SUPPORTIVE"
    else:
        state = "NEUTRAL"

    block = state == "RED_FLAG" and charm in {"high", "moderate"}

    if state == "RED_FLAG":
        reasons.insert(
            0,
            "Red Flag: upside call hedging / dealer short positioning — rallies may fail "
            "into resistance (VolSignals framework, proxy data)",
        )

    return {
        "symbol": symbol,
        "state": state,
        "score": round(score, 1),
        "expiry": exp,
        "spot": round(float(spot), 4),
        "otm_call_oi_share": round(otm_share, 3),
        "otm_call_vol_share": round(vol_share, 3),
        "resistance_strikes": resistance_strikes,
        "equilibrium_strike": pin,
        "charm_pressure": charm,
        "minutes_to_close": round(mins_left, 1) if mins_left is not None else None,
        "block_0dte_long_calls": block,
        "reasons": reasons,
        "strategy_hint": _strategy_hint(state),
        "volsignals_note": VOLSIGNALS_NOTE,
        "bottom_line_rules": BOTTOM_LINE_RULES,
        "proxy": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _strategy_hint(state: RedFlagState) -> str:
    if state == "RED_FLAG":
        return (
            "Range / fade framework: expect rips to stall into call-wall strikes. "
            "Prefer defined-risk structures (call flies / put flies) or wait for "
            "reaction — avoid chasing 0DTE long calls into resistance."
        )
    if state == "SUPPORTIVE":
        return "Mild supportive positioning — rallies may hold better, still use tape confirm."
    return "Neutral dealer proxy — use standard tape + quality gates."


def apply_red_flag_to_actions(
    actions: dict[str, Any],
    red_flag: dict[str, Any] | None,
    *,
    index_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """Downgrade 0DTE BUY_NOW when index Red Flag is active."""
    if not red_flag or not red_flag.get("block_0dte_long_calls"):
        return actions

    idx = index_symbols or {"SPY", "QQQ", "IWM", "SPX", "XSP", "DIA"}
    state = red_flag.get("state", "NEUTRAL")
    rf_sym = str(red_flag.get("symbol") or "SPY")

    def _patch(sig: dict[str, Any]) -> dict[str, Any]:
        sym = str(sig.get("symbol") or "")
        bucket = sig.get("dte_bucket") or "0dte"
        if sig.get("action") != "BUY_NOW":
            return sig
        if bucket != "0dte" and sig.get("dte") not in (0, 1, None):
            return sig
        # Block index 0DTE longs; warn on high-beta singles when SPY red flag
        if sym in idx or (state == "RED_FLAG" and sym in idx):
            out = dict(sig)
            out["action"] = "WAIT"
            out["headline"] = out.get("headline", "").replace("BUY NOW", "WAIT", 1) or "WAIT — Red Flag"
            out["detail"] = (
                f"{out.get('detail', '')} · Red Flag on {rf_sym} ({state}): "
                "0DTE long calls blocked — dealer/charm proxy caps rally risk"
            ).strip(" ·")
            out["strength"] = min(float(out.get("strength") or 50), 48.0)
            out["red_flag_blocked"] = True
            return out
        return sig

    out = dict(actions)
    for key in ("all", "buy_now", "buy_now_0dte", "buy_now_weekly", "wait", "hold"):
        if key in out and isinstance(out[key], list):
            out[key] = [_patch(s) for s in out[key]]

    primary = out.get("primary")
    if isinstance(primary, dict):
        out["primary"] = _patch(primary)

    counts = dict(out.get("counts") or {})
    out["counts"] = counts
    out["red_flag"] = {
        "active": True,
        "state": state,
        "symbol": rf_sym,
        "score": red_flag.get("score"),
        "charm_pressure": red_flag.get("charm_pressure"),
        "equilibrium_strike": red_flag.get("equilibrium_strike"),
        "blocked_0dte_index_longs": True,
    }
    return out
