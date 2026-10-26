"""SPX 0DTE strategy selector.

Levels freeze on the first chain of an expiry. The map is:

- Expected move (1σ) is the at-the-money straddle, call mid plus put mid.
  If that quote is missing, ``spot * atm_iv * sqrt(minutes_left / minutes_in_year)``.
- UT1 / LT1 = anchor ± 0.5 × expected move.
- UT2 / LT2 = anchor ± 1.0 × expected move. That pair is the expected-move band.
- UT3 / LT3 = anchor ± 1.5 × expected move.
- A level snaps to a high open-interest strike when one sits within 20% of the
  expected move. Otherwise it rounds to the strike step (5 for SPX, 1 for XSP).
- The pin zone is the open-interest and gamma cluster nearest the anchor,
  inside about a third of the expected move. The zone runs from the first to
  the last strike in that cluster.

Volatility is rich when at-the-money IV is at least 1.15× the 20-day realized
vol, cheap at or below 0.85×, and fair in between. A missing print is unknown.
Missing 5-minute momentum is never treated as a fade or as a quiet tape.

Strategies are paper signals only. They are not routed to Webull.

- Long call or put when the trend is strong and IV is not rich. When IV is
  rich the same trend becomes a debit spread.
- Short iron condor when IV is rich and spot is inside the pin with a quiet
  tape. Short strikes sit at or outside UT2 / LT2. Wings are a fixed width.
- Short call credit spread when spot tags UT3 and 5-minute momentum has faded.
  The put credit is the mirror at LT3.

No new condor after 1:00 PM CT. No new long premium after 2:00 PM CT. No new
credit spread after 2:15 PM CT. Open structures are marked flat at 2:45 PM CT.
Defined-risk ideas risk 1% of account equity. Long premium risks 0.5%.
Paper P&L is a model mark, not a fill. Take-profit on credits books 60% of
the credit, inside the 50–70% rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import CT

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]

SIGNAL_NAMES = {
    "long_call": "SPX Long Call",
    "long_put": "SPX Long Put",
    "call_debit": "SPX Call Debit",
    "put_debit": "SPX Put Debit",
    "iron_condor": "SPX Iron Condor",
    "call_credit": "SPX Call Credit",
    "put_credit": "SPX Put Credit",
}

DEFINITIONS = (
    "Expected move is the 0DTE at-the-money straddle (call mid + put mid). "
    "UT1/LT1 are half that move, UT2/LT2 are the full expected-move band, and "
    "UT3/LT3 are 1.5×. Each level snaps to a nearby high open-interest strike "
    "when one sits within 20% of the move. The pin is the largest open-interest "
    "and gamma cluster near the anchor. Levels freeze on the first chain of that expiry."
)

PAPER_NOTE = (
    "Paper structure marks only. Dollars are per one-lot model mark, not a broker fill "
    "and not a Webull order. A win is a closed mark above zero. Open marks stay open."
)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or math.isinf(number):
        return None
    return number


def _strike_step(symbol: str) -> float:
    return 1.0 if str(symbol).upper() == "XSP" else 5.0


def _wing(symbol: str, configured: float | None) -> float:
    if str(symbol).upper() == "XSP":
        return 2.0
    if configured is not None and configured > 0:
        return float(configured)
    return 10.0


def _k(strike: float) -> float:
    return round(float(strike), 2)


def _fmt_strike(strike: float) -> str:
    if abs(strike - round(strike)) < 0.01:
        return str(int(round(strike)))
    return f"{strike:.2f}"


def _money(value: float) -> str:
    return f"${value:.2f}"


def session_phase(now: datetime) -> str:
    """Chicago session gate for new structures and the 2:45 PM flat."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=CT)
    local = now.astimezone(CT)
    if local.weekday() >= 5:
        return "closed"
    minutes = local.hour * 60 + local.minute
    if minutes < 8 * 60 + 30:
        return "premarket"
    if minutes < 9 * 60 + 45:
        return "open_drive"
    if minutes < 13 * 60:
        return "core"
    if minutes < 14 * 60:
        return "afternoon"
    if minutes < 14 * 60 + 15:
        return "late"
    if minutes < 14 * 60 + 45:
        return "flatten_only"
    if minutes < 15 * 60:
        return "flatten"
    return "closed"


def _iv_decimal(value: Any) -> float | None:
    number = _num(value)
    if number is None or number <= 0:
        return None
    if number > 3:
        number = number / 100.0
    if number > 3 or number < 0.01:
        return None
    return number


def normalize_option(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One chain row from Tradier or a Polygon snapshot into a common quote."""
    if not isinstance(raw, dict):
        return None
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    greeks = raw.get("greeks") if isinstance(raw.get("greeks"), dict) else {}
    quote = raw.get("last_quote") if isinstance(raw.get("last_quote"), dict) else {}
    right_raw = str(
        raw.get("option_type")
        or raw.get("type")
        or raw.get("right")
        or details.get("contract_type")
        or ""
    ).lower()
    if right_raw.startswith("c"):
        right = "C"
    elif right_raw.startswith("p"):
        right = "P"
    else:
        return None
    strike = _num(raw.get("strike") if raw.get("strike") is not None else details.get("strike_price"))
    if strike is None or strike <= 0:
        return None
    bid = _num(raw.get("bid") if raw.get("bid") is not None else quote.get("bid") or quote.get("b")) or 0.0
    ask = _num(raw.get("ask") if raw.get("ask") is not None else quote.get("ask") or quote.get("a")) or 0.0
    last = _num(raw.get("last") or raw.get("last_price"))
    if last is None:
        trade = raw.get("last_trade") if isinstance(raw.get("last_trade"), dict) else {}
        day = raw.get("day") if isinstance(raw.get("day"), dict) else {}
        last = _num(trade.get("price") or day.get("close"))
    if bid < 0:
        bid = 0.0
    if ask < 0:
        ask = 0.0
    if ask > 0 and bid > ask:
        bid, ask = ask, bid
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else (last if last and last > 0 else 0.0)
    oi = int(_num(raw.get("open_interest") if raw.get("open_interest") is not None else raw.get("oi")) or 0)
    iv = _iv_decimal(
        greeks.get("mid_iv")
        or greeks.get("smv_vol")
        or raw.get("iv")
        or raw.get("implied_volatility")
        or greeks.get("iv")
    )
    gamma = _num(greeks.get("gamma") if greeks.get("gamma") is not None else raw.get("gamma"))
    return {
        "strike": _k(strike),
        "right": right,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "oi": max(0, oi),
        "iv": iv,
        "gamma": gamma,
    }


def _by_strike(options: list[dict[str, Any]], strike: float, right: str) -> dict[str, Any] | None:
    want = _k(strike)
    for row in options:
        if row.get("right") == right and _k(row["strike"]) == want:
            return row
    return None


def _listed(options: list[dict[str, Any]], right: str) -> list[float]:
    found = {
        _k(row["strike"])
        for row in options
        if row.get("right") == right and ((row.get("bid") or 0) > 0 or (row.get("ask") or 0) > 0)
    }
    return sorted(found)


def realized_vol(closes: list[float], window: int) -> float | None:
    """Annualized close-to-close realized vol. Decimal, not percent."""
    if window < 2 or len(closes) < window + 1:
        return None
    tail = closes[-(window + 1) :]
    rets: list[float] = []
    for left, right in zip(tail, tail[1:]):
        if left <= 0 or right <= 0:
            return None
        rets.append(math.log(right / left))
    if len(rets) < window:
        return None
    mean = sum(rets) / len(rets)
    var = sum((item - mean) ** 2 for item in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def _spot_from_parity(options: list[dict[str, Any]]) -> float | None:
    """Strike where the call and put mids are closest. A stand-in when the index quote is missing."""
    sides: dict[float, dict[str, dict[str, Any]]] = {}
    for row in options:
        sides.setdefault(_k(row["strike"]), {})[row["right"]] = row
    best = None
    best_gap = 1e18
    for strike, pair in sides.items():
        call = pair.get("C")
        put = pair.get("P")
        if not call or not put or call["mid"] <= 0 or put["mid"] <= 0:
            continue
        gap = abs(call["mid"] - put["mid"])
        if gap < best_gap:
            best_gap = gap
            best = strike
    return best


def _atm_straddle(options: list[dict[str, Any]], spot: float) -> dict[str, Any] | None:
    strikes = sorted({_k(row["strike"]) for row in options})
    if not strikes or spot <= 0:
        return None
    strike = min(strikes, key=lambda item: abs(item - spot))
    call = _by_strike(options, strike, "C")
    put = _by_strike(options, strike, "P")
    if not call or not put:
        return None
    call_mid = call["mid"] if call["mid"] > 0 else 0.0
    put_mid = put["mid"] if put["mid"] > 0 else 0.0
    if call_mid <= 0 or put_mid <= 0:
        return None
    ivs = [item for item in (call.get("iv"), put.get("iv")) if item]
    return {
        "strike": strike,
        "straddle": call_mid + put_mid,
        "iv": (sum(ivs) / len(ivs)) if ivs else None,
    }


def _minutes_left(now: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(ET)
    close = datetime.combine(local.date(), time(16, 0), tzinfo=ET)
    remaining = (close - local).total_seconds() / 60.0
    if remaining < 30:
        # After the cash close the listed chain is the next session, a full day.
        return 390.0
    return remaining


def compute_levels(
    spot: float,
    options: list[dict[str, Any]],
    *,
    symbol: str,
    now: datetime,
    anchor: float | None = None,
) -> dict[str, Any]:
    """Build the frozen map from a chain. ``anchor`` defaults to ``spot``."""
    ref = float(anchor if anchor is not None else spot)
    step = _strike_step(symbol)
    atm = _atm_straddle(options, ref)
    em = None
    em_source = None
    atm_iv = None
    atm_strike = None
    if atm and atm["straddle"] > 0.5:
        em = float(atm["straddle"])
        em_source = "atm_straddle"
        atm_iv = atm.get("iv")
        atm_strike = atm["strike"]
    if em is None and atm and atm.get("iv"):
        minutes = _minutes_left(now)
        em = ref * float(atm["iv"]) * math.sqrt(minutes / (365.0 * 24.0 * 60.0))
        em_source = "atm_iv"
        atm_iv = atm.get("iv")
        atm_strike = atm["strike"]
    if em is None or em <= 0:
        raise ValueError("expected move is unavailable")

    def snap(raw: float) -> float:
        rounded = round(raw / step) * step
        oi: dict[float, float] = {}
        for row in options:
            key = _k(row["strike"])
            oi[key] = oi.get(key, 0.0) + float(row.get("oi") or 0)
        if not oi:
            return _k(rounded)
        vals = sorted(oi.values())
        cutoff = vals[len(vals) // 2]
        band = max(0.20 * em, step)
        near = [key for key, size in oi.items() if size >= cutoff and size > 0 and abs(key - raw) <= band]
        if not near:
            return _k(rounded)
        return min(near, key=lambda key: (abs(key - raw), -oi[key]))

    ut1, ut2, ut3 = snap(ref + 0.5 * em), snap(ref + em), snap(ref + 1.5 * em)
    lt1, lt2, lt3 = snap(ref - 0.5 * em), snap(ref - em), snap(ref - 1.5 * em)
    # Keep the ladder ordered if two snaps land on the same strike.
    if ut2 <= ut1:
        ut2 = _k(ut1 + step)
    if ut3 <= ut2:
        ut3 = _k(ut2 + step)
    if lt2 >= lt1:
        lt2 = _k(lt1 - step)
    if lt3 >= lt2:
        lt3 = _k(lt2 - step)

    window = max(0.35 * em, 3 * step)
    scores: dict[float, float] = {}
    for row in options:
        key = _k(row["strike"])
        if abs(key - ref) > window:
            continue
        gamma = abs(row["gamma"]) if row.get("gamma") else 0.0
        scores[key] = scores.get(key, 0.0) + float(row.get("oi") or 0) + gamma * max(row.get("oi") or 0, 1) * 100.0
    pin = None
    if scores:
        peak = max(scores, key=lambda key: scores[key])
        thresh = 0.60 * scores[peak]
        cluster = sorted(
            key for key, size in scores.items() if size >= thresh and abs(key - peak) <= 2 * step
        )
        if not cluster:
            cluster = [peak]
        pin = {
            "strike": peak,
            "low": cluster[0],
            "high": cluster[-1],
        }

    return {
        "symbol": str(symbol).upper(),
        "anchor_spot": round(ref, 2),
        "em": round(em, 2),
        "em_source": em_source,
        "atm_strike": atm_strike,
        "atm_iv": round(atm_iv, 4) if atm_iv else None,
        "ut1": ut1,
        "ut2": ut2,
        "ut3": ut3,
        "lt1": lt1,
        "lt2": lt2,
        "lt3": lt3,
        "band_low": lt2,
        "band_high": ut2,
        "pin": pin,
        "definitions": DEFINITIONS,
    }


def classify_regime(
    *,
    atm_iv: float | None,
    rv20: float | None,
    rv5: float | None,
    session_change: float | None,
    mom5: float | None,
    spot: float,
    levels: dict[str, Any],
) -> dict[str, Any]:
    """IV rich/fair/cheap plus trend. Missing 5-minute momentum does not fade."""
    ratio = None
    if atm_iv and rv20 and rv20 > 0:
        ratio = atm_iv / rv20
    if ratio is None:
        iv_label = "unknown"
    elif ratio >= 1.15:
        iv_label = "rich"
    elif ratio <= 0.85:
        iv_label = "cheap"
    else:
        iv_label = "fair"

    em = float(levels.get("em") or 0)
    buffer = max(0.0015 * spot, 0.15 * em) if spot else 0.0
    ut3 = float(levels["ut3"])
    lt3 = float(levels["lt3"])
    near_high = spot >= ut3 - buffer
    near_low = spot <= lt3 + buffer
    if near_high and mom5 is not None and mom5 <= 0.02:
        trend = "fade_high"
    elif near_low and mom5 is not None and mom5 >= -0.02:
        trend = "fade_low"
    elif near_high and mom5 is not None and mom5 > 0.02:
        trend = "strong_up"
    elif near_low and mom5 is not None and mom5 < -0.02:
        trend = "strong_down"
    elif (near_high or near_low) and mom5 is None:
        trend = "extended"
    elif session_change is not None and session_change >= 0.30 and (mom5 is None or mom5 >= 0):
        trend = "strong_up"
    elif session_change is not None and session_change <= -0.30 and (mom5 is None or mom5 <= 0):
        trend = "strong_down"
    else:
        inside = float(levels["lt1"]) <= spot <= float(levels["ut1"])
        quiet_session = session_change is not None and abs(session_change) < 0.35
        quiet_mom = mom5 is not None and abs(mom5) < 0.08
        pin = levels.get("pin") or {}
        in_pin = bool(pin) and float(pin["low"]) <= spot <= float(pin["high"])
        if (inside or in_pin) and (quiet_mom or (quiet_session and mom5 is not None)):
            trend = "range"
        elif session_change is None and mom5 is None:
            trend = "unknown"
        else:
            trend = "mixed"

    return {
        "iv": iv_label,
        "iv_ratio": round(ratio, 3) if ratio is not None else None,
        "atm_iv": round(atm_iv, 4) if atm_iv else None,
        "rv20": round(rv20, 4) if rv20 else None,
        "rv5": round(rv5, 4) if rv5 else None,
        "trend": trend,
        "session_change_pct": session_change,
        "mom_5m_pct": mom5,
    }


def _size(max_loss: float, *, long_premium: bool, risk_defined: float, risk_long: float) -> dict[str, Any]:
    pct = risk_long if long_premium else risk_defined
    return {
        "risk_pct": pct,
        "max_loss_per_contract": round(max_loss, 2),
        "formula": f"contracts = floor(account × {pct:.1%} / {max_loss:.0f})",
    }


def _gate(action: str, phase: str, strategy: str) -> tuple[str, str]:
    if phase in {"premarket", "closed"}:
        return "WATCH", "outside the cash session — levels only, no new entry"
    if phase == "flatten":
        return "FLAT", "2:45 PM CT flat — no new risk"
    if strategy == "iron_condor":
        if phase != "core":
            return "WATCH", "no new iron condor after 1:00 PM CT or during the open drive"
        return action, ""
    if strategy in {"long_call", "long_put", "call_debit", "put_debit"}:
        if phase in {"late", "flatten_only", "flatten"}:
            return "WATCH", "no new long premium after 2:00 PM CT"
        return action, ""
    if strategy in {"call_credit", "put_credit"}:
        if phase in {"open_drive", "flatten_only", "flatten"}:
            return "WATCH", "credit spread waits until 9:45 AM CT and stops at 2:15 PM CT"
        if phase == "late":
            return "WATCH", "no new credit spread after 2:15 PM CT"
        return action, ""
    return action, ""


def _structure_label(legs: list[tuple[float, float]]) -> str:
    parts = [f"{_fmt_strike(a)}/{_fmt_strike(b)}" for a, b in legs]
    return " – ".join(parts)


def _exit_text(strategy: str) -> str:
    if strategy == "iron_condor":
        return "take 50–70% of credit (paper books 60%) · stop if spot trades through a short strike · flat 2:45 PM CT"
    if strategy in {"call_credit", "put_credit"}:
        return "take 50–70% of credit (paper books 60%) · stop on a break through the short strike · flat 2:45 PM CT"
    if strategy in {"call_debit", "put_debit"}:
        return "take 50% of debit in premium or the UT2/LT2 target · stop if premium loses 40% or spot loses the anchor · flat 2:45 PM CT"
    return "take the UT2/LT2 target or +50% of premium · stop if premium loses 40% or spot loses the anchor · flat 2:45 PM CT"


def _message(symbol: str, strategy: str, label: str, price: float, price_kind: str, max_loss: float) -> str:
    name = {
        "iron_condor": "IRON CONDOR",
        "call_credit": "CALL CREDIT",
        "put_credit": "PUT CREDIT",
        "call_debit": "CALL DEBIT",
        "put_debit": "PUT DEBIT",
        "long_call": "LONG CALL",
        "long_put": "LONG PUT",
    }[strategy]
    mark = "🦅" if strategy in {"iron_condor", "call_credit", "put_credit"} else ("🟢" if "call" in strategy else "🔴")
    return (
        f"{mark} {symbol} {name} {label} {price_kind} {_money(price)}, "
        f"max loss {_money(max_loss)}, exit {_exit_text(strategy)}"
    )


def select_strategies(
    levels: dict[str, Any],
    regime: dict[str, Any],
    options: list[dict[str, Any]],
    *,
    now: datetime,
    symbol: str,
    wing: float = 10.0,
    risk_defined: float = 0.01,
    risk_long: float = 0.005,
) -> list[dict[str, Any]]:
    """Concrete structures for this tape. Action is ENTER, WATCH, or FLAT."""
    phase = session_phase(now)
    symbol = str(symbol).upper()
    trend = str(regime.get("trend") or "unknown")
    iv = str(regime.get("iv") or "unknown")
    expiry = levels.get("expiry")
    day = str(levels.get("day") or "")
    setups: list[dict[str, Any]] = []

    def finish(setup: dict[str, Any]) -> None:
        action, why = _gate("ENTER", phase, setup["strategy"])
        if why:
            setup["reason"] = f"{setup['reason']} · {why}" if setup.get("reason") else why
        if phase == "flatten":
            action = "FLAT"
        setup["action"] = action
        setup["phase"] = phase
        setups.append(setup)

    calls = _listed(options, "C")
    puts = _listed(options, "P")

    def credit_spread(strategy: str, short_k: float | None, long_k: float | None, right: str) -> None:
        if short_k is None or long_k is None:
            return
        short = _by_strike(options, short_k, right)
        long = _by_strike(options, long_k, right)
        if not short or not long or short["bid"] <= 0 or long["ask"] <= 0:
            return
        credit = short["bid"] - long["ask"]
        width = abs(long_k - short_k)
        if credit <= 0.05 or credit >= width:
            return
        max_loss = (width - credit) * 100.0
        if strategy == "call_credit":
            label = f"{_fmt_strike(short_k)}/{_fmt_strike(long_k)}"
        else:
            label = f"{_fmt_strike(long_k)}/{_fmt_strike(short_k)}"
        size = _size(max_loss, long_premium=False, risk_defined=risk_defined, risk_long=risk_long)
        reason = "IV is not cheap and momentum faded at the stretch target"
        if iv == "unknown":
            reason = "momentum faded at the stretch target, but IV history is missing — watch only"
        setup = {
            "strategy": strategy,
            "symbol": symbol,
            "expiry": expiry,
            "day": day,
            "price_kind": "credit",
            "credit": round(credit, 2),
            "debit": None,
            "max_loss": round(max_loss, 2),
            "max_profit": round(credit * 100.0, 2),
            "structure": label,
            "legs": [
                {"right": right, "strike": short_k, "side": "short"},
                {"right": right, "strike": long_k, "side": "long"},
            ],
            "reason": reason,
            "exit": _exit_text(strategy),
            "size": size,
            "message": _message(symbol, strategy, label, credit, "credit", max_loss),
        }
        if iv == "cheap":
            return
        if iv == "unknown":
            setup["action"] = "WATCH"
            setup["phase"] = phase
            setups.append(setup)
            return
        finish(setup)

    if trend == "fade_high":
        short_k = next((item for item in calls if item >= float(levels["ut3"]) - 1e-6), None)
        long_k = None
        if short_k is not None:
            target = short_k + wing
            long_k = next((item for item in calls if item >= target - 1e-6), None)
        credit_spread("call_credit", short_k, long_k, "C")
    if trend == "fade_low":
        short_k = next((item for item in reversed(puts) if item <= float(levels["lt3"]) + 1e-6), None)
        long_k = None
        if short_k is not None:
            target = short_k - wing
            below = [item for item in puts if item <= target + 1e-6]
            long_k = below[-1] if below else None
        credit_spread("put_credit", short_k, long_k, "P")

    if trend == "range" and iv == "rich":
        call_short = next((item for item in calls if item >= float(levels["ut2"]) - 1e-6), None)
        put_short = next((item for item in reversed(puts) if item <= float(levels["lt2"]) + 1e-6), None)
        call_long = None
        put_long = None
        if call_short is not None:
            call_long = next((item for item in calls if item >= call_short + wing - 1e-6), None)
        if put_short is not None:
            below = [item for item in puts if item <= put_short - wing + 1e-6]
            put_long = below[-1] if below else None
        if None not in {call_short, call_long, put_short, put_long}:
            pieces = [
                (_by_strike(options, put_long, "P"), "ask"),
                (_by_strike(options, put_short, "P"), "bid"),
                (_by_strike(options, call_short, "C"), "bid"),
                (_by_strike(options, call_long, "C"), "ask"),
            ]
            if all(row is not None and row[side] > 0 for row, side in pieces):
                credit = pieces[1][0]["bid"] + pieces[2][0]["bid"] - pieces[0][0]["ask"] - pieces[3][0]["ask"]
                width = min(abs(call_long - call_short), abs(put_short - put_long))
                if credit > 0.05 and credit < width:
                    max_loss = (width - credit) * 100.0
                    label = _structure_label([(put_long, put_short), (call_short, call_long)])
                    size = _size(max_loss, long_premium=False, risk_defined=risk_defined, risk_long=risk_long)
                    finish(
                        {
                            "strategy": "iron_condor",
                            "symbol": symbol,
                            "expiry": expiry,
                            "day": day,
                            "price_kind": "credit",
                            "credit": round(credit, 2),
                            "debit": None,
                            "max_loss": round(max_loss, 2),
                            "max_profit": round(credit * 100.0, 2),
                            "structure": label,
                            "legs": [
                                {"right": "P", "strike": put_long, "side": "long"},
                                {"right": "P", "strike": put_short, "side": "short"},
                                {"right": "C", "strike": call_short, "side": "short"},
                                {"right": "C", "strike": call_long, "side": "long"},
                            ],
                            "reason": "IV rich and spot is inside the pin with a quiet tape",
                            "exit": _exit_text("iron_condor"),
                            "size": size,
                            "message": _message(symbol, "iron_condor", label, credit, "credit", max_loss),
                        }
                    )

    def directional(strategy: str, right: str, bullish: bool) -> None:
        pool = calls if right == "C" else puts
        if not pool:
            return
        spot = float(levels.get("spot") or levels["anchor_spot"])
        if bullish:
            buy_k = max([item for item in pool if item <= spot + 1e-6], default=pool[0])
        else:
            buy_k = min([item for item in pool if item >= spot - 1e-6], default=pool[-1])
        buy = _by_strike(options, buy_k, right)
        if not buy or buy["ask"] <= 0:
            return
        rich = iv == "rich"
        sell_k = None
        sell = None
        if rich:
            target = float(levels["ut2"] if bullish else levels["lt2"])
            if bullish:
                sell_k = next((item for item in pool if item >= max(target, buy_k + _strike_step(symbol)) - 1e-6), None)
            else:
                below = [item for item in pool if item <= min(target, buy_k - _strike_step(symbol)) + 1e-6]
                sell_k = below[-1] if below else None
            sell = _by_strike(options, sell_k, right) if sell_k is not None else None
            if not sell or sell["bid"] <= 0:
                return
            debit = buy["ask"] - sell["bid"]
            width = abs(sell_k - buy_k)
            if debit <= 0.05 or debit >= width:
                return
            max_loss = debit * 100.0
            label = f"{_fmt_strike(min(buy_k, sell_k))}/{_fmt_strike(max(buy_k, sell_k))}"
            kind = "call_debit" if bullish else "put_debit"
            legs = [
                {"right": right, "strike": buy_k, "side": "long"},
                {"right": right, "strike": sell_k, "side": "short"},
            ]
        else:
            debit = buy["ask"]
            max_loss = debit * 100.0
            label = _fmt_strike(buy_k)
            kind = strategy
            legs = [{"right": right, "strike": buy_k, "side": "long"}]
        size = _size(max_loss, long_premium=True, risk_defined=risk_defined, risk_long=risk_long)
        reason = "trend is strong and premium is not rich" if not rich else "trend is strong and IV is rich — debit spread instead of a naked long"
        finish(
            {
                "strategy": kind,
                "symbol": symbol,
                "expiry": expiry,
                "day": day,
                "price_kind": "debit",
                "credit": None,
                "debit": round(debit, 2),
                "max_loss": round(max_loss, 2),
                "max_profit": None if not rich else round((width - debit) * 100.0, 2),
                "structure": label,
                "legs": legs,
                "reason": reason,
                "exit": _exit_text(kind),
                "size": size,
                "message": _message(symbol, kind, label, debit, "debit", max_loss),
            }
        )

    if trend == "strong_up":
        directional("long_call", "C", True)
    elif trend == "strong_down":
        directional("long_put", "P", False)

    return setups


def _cost_to_close(trade: dict[str, Any], options: list[dict[str, Any]]) -> float | None:
    """Debit required to close a credit, or credit received to close a debit."""
    total = 0.0
    for leg in trade.get("legs") or []:
        row = _by_strike(options, float(leg["strike"]), str(leg["right"]))
        if row is None:
            return None
        if trade["price_kind"] == "credit":
            # Buy back shorts at the ask, sell longs at the bid.
            px = row["ask"] if leg["side"] == "short" else -row["bid"]
        else:
            px = row["bid"] if leg["side"] == "long" else -row["ask"]
        if leg["side"] == "short" and row["ask"] <= 0:
            return None
        if leg["side"] == "long" and row["bid"] < 0:
            return None
        total += px
    return total


def mark_trade(
    trade: dict[str, Any],
    options: list[dict[str, Any]],
    *,
    spot: float,
    levels: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    """Update model P&L. Close on 60% credit, a level break, or the 2:45 PM flat."""
    out = dict(trade)
    out["legs"] = list(trade.get("legs") or [])
    if out.get("status") == "closed":
        return out
    entry = float(out.get("entry") or 0)
    mark = _cost_to_close(out, options)
    if mark is not None:
        if out["price_kind"] == "credit":
            pnl = (entry - mark) * 100.0
        else:
            pnl = (mark - entry) * 100.0
        out["mark"] = round(mark, 2)
        out["pnl"] = round(pnl, 2)
    em = float(levels.get("em") or 0)
    reason = None
    if phase == "flatten":
        reason = "flatten"
    elif out["strategy"] in {"call_credit", "put_credit", "iron_condor"} and entry > 0 and mark is not None:
        if mark <= entry * 0.40:
            reason = "take_profit"
    elif out["price_kind"] == "debit" and entry > 0 and mark is not None:
        if mark >= entry * 1.50:
            reason = "take_profit"
        elif mark <= entry * 0.60:
            reason = "stop"
    if reason is None and out["strategy"] == "call_credit":
        short = next(leg["strike"] for leg in out["legs"] if leg["side"] == "short")
        if spot > float(short):
            reason = "stop"
    elif reason is None and out["strategy"] == "put_credit":
        short = next(leg["strike"] for leg in out["legs"] if leg["side"] == "short")
        if spot < float(short):
            reason = "stop"
    elif reason is None and out["strategy"] == "iron_condor":
        call_short = next(leg["strike"] for leg in out["legs"] if leg["side"] == "short" and leg["right"] == "C")
        put_short = next(leg["strike"] for leg in out["legs"] if leg["side"] == "short" and leg["right"] == "P")
        if spot >= float(call_short) or spot <= float(put_short):
            reason = "stop"
    elif reason is None and out["strategy"] in {"long_call", "call_debit"}:
        anchor = float(levels.get("anchor_spot") or spot)
        if spot < anchor:
            reason = "stop"
        elif spot >= float(levels["ut2"]):
            reason = "take_profit"
    elif reason is None and out["strategy"] in {"long_put", "put_debit"}:
        anchor = float(levels.get("anchor_spot") or spot)
        if spot > anchor:
            reason = "stop"
        elif spot <= float(levels["lt2"]):
            reason = "take_profit"
    if reason and em < 0:
        reason = None
    if reason:
        out["status"] = "closed"
        out["exit_reason"] = reason
        if out.get("pnl") is None:
            out["win"] = None
        else:
            out["win"] = bool(out["pnl"] > 0)
    else:
        out["status"] = "open"
        out["win"] = None
    return out


def _signal_id(day: str, strategy: str, structure: str) -> str:
    raw = json.dumps(
        {"day": day, "strategy": strategy, "structure": structure, "v": 1},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _trade_payload(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 1,
        "id": trade["id"],
        "signaled_at": trade["opened_at"],
        "signaled_at_ct": trade.get("opened_at_ct"),
        "recorded_at": trade["opened_at"],
        "ticker": trade["symbol"],
        "signal_type": SIGNAL_NAMES.get(trade["strategy"], trade["strategy"]),
        "strategy": trade["strategy"],
        "direction": "credit" if trade["price_kind"] == "credit" else "debit",
        "reference_entry": trade["entry"],
        "reference_basis": "structure",
        "premium_reference": trade["entry"],
        "contract": trade.get("structure"),
        "max_loss": trade.get("max_loss"),
        "max_profit": trade.get("max_profit"),
        "legs": trade.get("legs"),
        "exit": trade.get("exit"),
        "risk_pct": (trade.get("size") or {}).get("risk_pct"),
        "day": trade.get("day"),
        "expiry": trade.get("expiry"),
    }


def _trade_outcome(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "structure",
        "strategy": trade.get("strategy"),
        "status": trade.get("status") or "open",
        "entry": trade.get("entry"),
        "mark": trade.get("mark"),
        "pnl": trade.get("pnl"),
        "win": trade.get("win"),
        "exit_reason": trade.get("exit_reason"),
    }


def update_book(
    state: dict[str, Any],
    setups: list[dict[str, Any]],
    options: list[dict[str, Any]],
    *,
    spot: float,
    levels: dict[str, Any],
    phase: str,
    now: datetime,
) -> dict[str, Any]:
    """Mark open paper structures and open a new ENTER once per strategy per day."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    opened = now.astimezone(timezone.utc).isoformat()
    opened_ct = now.astimezone(CT).strftime("%Y-%m-%d %H:%M CT")
    open_trades = [dict(row) for row in state.get("open") or []]
    closed = [dict(row) for row in state.get("closed") or []]
    used = {row.get("strategy") for row in open_trades + closed}
    marked: list[dict[str, Any]] = []
    for trade in open_trades:
        marked.append(mark_trade(trade, options, spot=spot, levels=levels, phase=phase))
    still_open = []
    for trade in marked:
        if trade.get("status") == "closed":
            trade["closed_at"] = opened
            closed.append(trade)
        else:
            still_open.append(trade)
    for setup in setups:
        if setup.get("action") != "ENTER":
            continue
        if setup.get("strategy") in used:
            continue
        entry = setup.get("credit") if setup.get("price_kind") == "credit" else setup.get("debit")
        if entry is None:
            continue
        trade = {
            "id": _signal_id(str(setup.get("day") or ""), setup["strategy"], setup["structure"]),
            "strategy": setup["strategy"],
            "symbol": setup["symbol"],
            "day": setup.get("day"),
            "expiry": setup.get("expiry"),
            "structure": setup["structure"],
            "price_kind": setup["price_kind"],
            "entry": float(entry),
            "mark": float(entry),
            "pnl": 0.0,
            "max_loss": setup.get("max_loss"),
            "max_profit": setup.get("max_profit"),
            "legs": setup.get("legs"),
            "exit": setup.get("exit"),
            "size": setup.get("size"),
            "status": "open",
            "win": None,
            "exit_reason": None,
            "opened_at": opened,
            "opened_at_ct": opened_ct,
        }
        still_open.append(trade)
        used.add(setup["strategy"])
    all_rows = still_open + closed
    return {
        "open": still_open,
        "closed": closed,
        "ledger_signals": [_trade_payload(row) for row in all_rows],
        "ledger_marks": [
            {"signal_id": row["id"], "outcome": _trade_outcome(row)} for row in all_rows
        ],
    }


def replay_proxy(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Underlying-path replay. This is not an option fill and not a premium P&L.

    Each day uses the prior close as the anchor and 20-day realized vol as the
    expected-move stand-in when no historical chain is available.
    """
    parsed: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        close = _num(bar.get("close"))
        high = _num(bar.get("high"))
        low = _num(bar.get("low"))
        opened = _num(bar.get("open"))
        if close is None or high is None or low is None or opened is None:
            continue
        parsed.append({"date": str(bar.get("date") or ""), "open": opened, "high": high, "low": low, "close": close})
    parsed.sort(key=lambda row: row["date"])
    closes = [row["close"] for row in parsed]
    scored = 0
    held = 0
    tagged = 0
    faded = 0
    trend_days = 0
    for index in range(21, len(parsed)):
        rv = realized_vol(closes[:index], 20)
        prev = closes[index - 1]
        if rv is None or prev <= 0:
            continue
        em = prev * rv * math.sqrt(1.0 / 365.0)
        if em <= 0:
            continue
        row = parsed[index]
        scored += 1
        if row["low"] >= prev - em and row["high"] <= prev + em:
            held += 1
        ut3 = prev + 1.5 * em
        if row["high"] >= ut3:
            tagged += 1
            if row["close"] < ut3:
                faded += 1
        if (row["close"] - row["open"]) > 0.5 * em and row["close"] > prev:
            trend_days += 1
    def rate(num: int, den: int) -> float | None:
        if den <= 0:
            return None
        return round(num / den, 4)

    return {
        "model": "underlying_path_proxy",
        "option_fills": False,
        "days": scored,
        "thin": scored < 30,
        "condor_range_held_rate": rate(held, scored),
        "ut3_tag_rate": rate(tagged, scored),
        "ut3_fade_rate": rate(faded, tagged),
        "trend_day_rate": rate(trend_days, scored),
        "note": (
            "Historical option chains were not replayed. Rates describe whether the "
            "underlying stayed inside a realized-vol expected move, tagged 1.5× that "
            "move, and closed back inside it. They are not credit captured and not edge."
        ),
    }


def _state_path(path: str | Path | None) -> Path:
    if path:
        return Path(path)
    return ROOT / "outputs" / "spx_strategy_state.json"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")


def _pick_expiry(dates: list[str], now: datetime) -> str | None:
    parsed: list[date] = []
    for raw in dates:
        try:
            parsed.append(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue
    parsed = sorted(set(parsed))
    if not parsed:
        return None
    local = now.astimezone(ET)
    session = local.date()
    if local.weekday() < 5 and local.time() < time(16, 5):
        for item in parsed:
            if item >= session:
                return item.isoformat()
    for item in parsed:
        if item > session:
            return item.isoformat()
    return parsed[-1].isoformat()


def _quote_change(row: dict[str, Any] | None) -> float | None:
    if not isinstance(row, dict):
        return None
    for key in ("change_percentage", "session_change_pct", "change_pct"):
        number = _num(row.get(key))
        if number is not None:
            return number
    return None


def fetch_chain(now: datetime) -> dict[str, Any]:
    """Tradier SPX, then XSP, then a Polygon snapshot. No secrets in the result."""
    from odte_scanner.data.tradier import fetch_expirations, fetch_option_chain, fetch_quotes

    errors: list[str] = []
    for symbol in ("SPX", "XSP"):
        exp = fetch_expirations(symbol)
        if not exp.get("ok"):
            errors.append(f"{symbol} expirations unavailable")
            continue
        expiry = _pick_expiry(list(exp.get("dates") or []), now)
        if not expiry:
            errors.append(f"{symbol} has no expiry")
            continue
        chain = fetch_option_chain(symbol, expiry, greeks=True)
        options = [row for row in (normalize_option(item) for item in chain.get("options") or []) if row]
        if not options:
            errors.append(f"{symbol} chain empty")
            continue
        quote = fetch_quotes([symbol])
        row = (quote.get("quotes") or {}).get(symbol) or {}
        spot = _num(row.get("last") or row.get("close"))
        if spot is None or spot <= 0:
            spot = None
        return {
            "ok": True,
            "symbol": symbol,
            "expiry": expiry,
            "spot": spot,
            "session_change_pct": _quote_change(row),
            "options": options,
            "source": "tradier",
        }
    try:
        from odte_scanner.data.polygon import fetch_option_chain_snapshot
    except Exception as exc:  # noqa: BLE001
        logger.debug("polygon import failed: %s", exc)
        fetch_option_chain_snapshot = None  # type: ignore
    if fetch_option_chain_snapshot is not None:
        # Next weekday expiry is enough when Tradier listed nothing.
        local = now.astimezone(ET).date()
        if now.astimezone(ET).time() >= time(16, 5) or local.weekday() >= 5:
            local = local + timedelta(days=1)
        while local.weekday() >= 5:
            local = local + timedelta(days=1)
        for underlying, symbol in (("I:SPX", "SPX"), ("I:XSP", "XSP")):
            snap = fetch_option_chain_snapshot(underlying, local.isoformat())
            options = [row for row in (normalize_option(item) for item in snap.get("options") or []) if row]
            if not options:
                continue
            return {
                "ok": True,
                "symbol": symbol,
                "expiry": local.isoformat(),
                "spot": None,
                "session_change_pct": None,
                "options": options,
                "source": "polygon",
            }
    return {"ok": False, "options": [], "error": "; ".join(errors) or "SPX and XSP chains unavailable", "source": None}


def _daily_closes(symbol: str) -> list[float]:
    from odte_scanner.data.fetcher import fetch_history

    yahoo = {"SPX": "^SPX", "XSP": "^XSP"}.get(symbol, symbol)
    frame = fetch_history(symbol, period="6mo", interval="1d", use_cache=True, cache_max_age_hours=6, yahoo_symbol=yahoo)
    if frame is None or getattr(frame, "empty", True):
        return []
    closes: list[float] = []
    for _, row in frame.iterrows():
        number = _num(row.get("Close") if hasattr(row, "get") else None)
        if number is not None and number > 0:
            closes.append(number)
    return closes


def _mom5(symbol: str) -> float | None:
    try:
        from datetime import timedelta as _td

        from odte_scanner.data.live_quotes import momentum_from_closes
        from odte_scanner.data.tradier import fetch_timesales

        moment = datetime.now(ET)
        start = (moment - _td(minutes=45)).strftime("%Y-%m-%d %H:%M")
        end = moment.strftime("%Y-%m-%d %H:%M")
        res = fetch_timesales(symbol, interval="5min", start=start, end=end, timeout=8.0)
        closes: list[float] = []
        for row in res.get("bars") or []:
            if not isinstance(row, dict):
                continue
            px = _num(row.get("close") or row.get("price"))
            if px and px > 0:
                closes.append(px)
        mom, _mom15 = momentum_from_closes(closes)
        return mom
    except Exception as exc:  # noqa: BLE001
        logger.debug("spx momentum skipped: %s", exc)
        return None


def build_spx_desk(
    *,
    cfg: dict[str, Any] | None = None,
    quotes: dict[str, Any] | None = None,
    now: datetime | None = None,
    chain: dict[str, Any] | None = None,
    bars: list[dict[str, Any]] | None = None,
    state_path: str | Path | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    """Levels, regime, setups, and the paper book for one expiry."""
    block = {}
    if isinstance(cfg, dict):
        raw = cfg.get("spx_strategy")
        block = raw if isinstance(raw, dict) else {}
    if block.get("enabled") is False:
        return {"ok": False, "disabled": True, "setups": [], "book": {"open": [], "closed": []}}
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    phase = session_phase(moment)
    path = _state_path(state_path)
    state = _load_state(path)
    limitations = [
        "Signal and paper-tracking only. These structures are not sent to Webull.",
        "Marks are model bid/ask, not fills.",
    ]
    fetched = chain
    if fetched is None and fetch:
        fetched = fetch_chain(moment)
    fetched = fetched or {}
    options = [row for row in (fetched.get("options") or []) if isinstance(row, dict) and row.get("strike")]
    if options and "bid" not in options[0]:
        options = [row for row in (normalize_option(item) for item in options) if row]
    if not options:
        return {
            "ok": False,
            "error": fetched.get("error") or "chain unavailable",
            "phase": phase,
            "setups": [],
            "levels": state.get("levels") if state.get("day") else None,
            "regime": None,
            "book": {"open": [], "closed": []},
            "limitations": limitations + ["No SPX or XSP chain, so no new levels."],
            "definitions": DEFINITIONS,
        }
    symbol = str(fetched.get("symbol") or "SPX").upper()
    expiry = str(fetched.get("expiry") or "")
    spot = _num(fetched.get("spot"))
    quote = (quotes or {}).get(symbol) if isinstance(quotes, dict) else None
    if isinstance(quote, dict):
        spot = _num(quote.get("last") or quote.get("session_last")) or spot
    if spot is None or spot <= 0:
        spot = _num((state.get("levels") or {}).get("anchor_spot"))
    spot_from_chain = False
    if spot is None or spot <= 0:
        spot = _spot_from_parity(options)
        spot_from_chain = spot is not None
    if spot is None or spot <= 0:
        return {
            "ok": False,
            "error": "spot unavailable",
            "phase": phase,
            "setups": [],
            "book": {"open": [], "closed": []},
            "limitations": limitations,
            "definitions": DEFINITIONS,
        }
    session = _quote_change(quote if isinstance(quote, dict) else None)
    if session is None:
        session = _num(fetched.get("session_change_pct"))
    mom5 = _num(quote.get("mom_5m_pct")) if isinstance(quote, dict) else None
    if mom5 is None:
        mom5 = _num(fetched.get("mom_5m_pct"))
    if mom5 is None and fetch and chain is None:
        mom5 = _mom5(symbol)

    same_day = state.get("day") == expiry and isinstance(state.get("levels"), dict)
    if same_day:
        levels = dict(state["levels"])
        rv20 = _num(state.get("rv20"))
        rv5 = _num(state.get("rv5"))
    else:
        closes: list[float] = []
        if bars is not None:
            closes = [float(row["close"]) for row in bars if _num(row.get("close"))]
        elif fetch:
            try:
                closes = _daily_closes(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug("spx realized vol skipped: %s", exc)
                closes = []
                limitations.append("Realized vol is unavailable, so IV is unknown and premium is not sold.")
        rv20 = realized_vol(closes, 20)
        rv5 = realized_vol(closes, 5)
        try:
            levels = compute_levels(spot, options, symbol=symbol, now=moment, anchor=spot)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "phase": phase,
                "setups": [],
                "book": {"open": [], "closed": []},
                "limitations": limitations,
                "definitions": DEFINITIONS,
            }
        anchor_kind = "premarket" if phase == "premarket" else ("rth" if phase not in {"closed", "flatten"} else "afterhours")
        levels["anchor_kind"] = anchor_kind
        state = {"day": expiry, "open": [], "closed": [], "rv20": rv20, "rv5": rv5}
    levels["symbol"] = symbol
    levels["expiry"] = expiry
    levels["day"] = expiry
    levels["spot"] = round(spot, 2)
    live_atm = _atm_straddle(options, spot)
    live_iv = live_atm.get("iv") if live_atm else None
    if live_iv is None:
        live_iv = _num(levels.get("atm_iv"))
    if fetched.get("source") == "polygon":
        limitations.append("Chain came from Polygon because Tradier had no SPX or XSP chain.")
    if symbol == "XSP":
        limitations.append("SPX chain was empty. Levels and strikes are XSP, about one tenth of SPX.")
    if spot_from_chain:
        limitations.append("Index quote was missing. Spot is the strike where the call and put mids meet.")
    if rv20 is None:
        limitations.append("20-day realized vol is missing, so the IV regime is unknown.")

    regime = classify_regime(
        atm_iv=live_iv,
        rv20=rv20,
        rv5=rv5,
        session_change=session,
        mom5=mom5,
        spot=spot,
        levels=levels,
    )
    wing = _wing(symbol, _num(block.get("wing_points")))
    risk_defined = _num(block.get("risk_pct_defined")) or 0.01
    risk_long = _num(block.get("risk_pct_long")) or 0.005
    setups = select_strategies(
        levels,
        regime,
        options,
        now=moment,
        symbol=symbol,
        wing=wing,
        risk_defined=risk_defined,
        risk_long=risk_long,
    )
    book = update_book(state, setups, options, spot=spot, levels=levels, phase=phase, now=moment)
    saved = {
        "day": expiry,
        "symbol": symbol,
        "expiry": expiry,
        "source": fetched.get("source"),
        "levels": {key: value for key, value in levels.items() if key != "spot"},
        "rv20": rv20,
        "rv5": rv5,
        "open": book["open"],
        "closed": book["closed"],
    }
    try:
        _save_state(path, saved)
    except OSError as exc:
        logger.warning("spx state save failed: %s", exc)
    return {
        "ok": True,
        "symbol": symbol,
        "expiry": expiry,
        "source": fetched.get("source"),
        "phase": phase,
        "spot": round(spot, 2),
        "levels": levels,
        "regime": regime,
        "setups": setups,
        "book": {"open": book["open"], "closed": book["closed"]},
        "ledger_signals": book["ledger_signals"],
        "ledger_marks": book["ledger_marks"],
        "definitions": DEFINITIONS,
        "paper_note": PAPER_NOTE,
        "limitations": limitations,
    }
