"""ML6 practical scoring: drawdown, earnings proximity, theme, liquidity, reaction gate."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from odte_scanner.ml6.watchlist import (
    STATUS_BUY_IF,
    STATUS_WAIT,
    STATUS_WATCH,
    ML6_WATCHLIST,
)

# Looser than 0DTE but still tradeable
MIN_PRICE = 1.50
MIN_AVG_VOLUME = 150_000  # shares (20d avg) — allows smaller names than 0DTE


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None


def days_to_earnings(earnings_date: str | date | None, *, asof: date | None = None) -> int | None:
    """Positive = days until print; negative = days since print."""
    d = _parse_day(earnings_date)
    if d is None:
        return None
    return (d - (asof or _today())).days


def drawdown_from_high(df: pd.DataFrame, *, lookback: int = 63) -> dict[str, float | None]:
    """Beaten-down bonus: % off recent high (negative = below high)."""
    if df is None or df.empty or "Close" not in df.columns:
        return {"drawdown_pct": None, "high": None, "last": None}
    window = df.tail(max(lookback, 5))
    last = float(window["Close"].iloc[-1])
    high = float(window["High"].max()) if "High" in window.columns else float(window["Close"].max())
    if high <= 0:
        return {"drawdown_pct": None, "high": high, "last": last}
    dd = (last / high - 1.0) * 100.0
    return {"drawdown_pct": dd, "high": high, "last": last}


def liquidity_metrics(df: pd.DataFrame) -> dict[str, float | None]:
    if df is None or df.empty:
        return {"avg_volume": None, "last_price": None, "passes": False}
    last = float(df["Close"].iloc[-1]) if "Close" in df.columns else None
    vol = None
    if "Volume" in df.columns and len(df) >= 5:
        vol = float(df["Volume"].tail(20).mean())
    passes = (
        last is not None
        and last >= MIN_PRICE
        and vol is not None
        and vol >= MIN_AVG_VOLUME
    )
    # Soft pass for missing volume (new listings) if price ok
    if last is not None and last >= MIN_PRICE and vol is None:
        passes = True
    return {"avg_volume": vol, "last_price": last, "passes": passes}


def realized_vol_pct(df: pd.DataFrame, *, window: int = 20) -> float | None:
    """Optional vol input when history exists — skip if thin."""
    if df is None or len(df) < window + 2 or "Close" not in df.columns:
        return None
    rets = df["Close"].pct_change().dropna().tail(window)
    if rets.empty:
        return None
    return float(rets.std() * (252 ** 0.5) * 100.0)


def theme_score(themes: list[str] | None) -> float:
    """0–100 theme fit for neocloud / AI infra / data-center / power."""
    if not themes:
        return 40.0
    wanted = {"neocloud", "ai_infra", "data_center", "power", "cloud"}
    hits = sum(1 for t in themes if str(t).lower() in wanted)
    # neocloud / ai_infra get a slight premium
    premium = 8.0 if any(str(t).lower() in {"neocloud", "ai_infra"} for t in themes) else 0.0
    return min(100.0, 45.0 + hits * 12.0 + premium)


def catalyst_score(days: int | None) -> float:
    """Peak interest near the print; still useful ± a few weeks."""
    if days is None:
        return 45.0
    ad = abs(days)
    if ad == 0:
        return 92.0
    if ad <= 2:
        return 88.0
    if ad <= 7:
        return 78.0
    if ad <= 14:
        return 70.0
    if ad <= 30:
        return 62.0
    if ad <= 60:
        return 52.0
    return 40.0


def drawdown_score(dd_pct: float | None) -> float:
    """Beaten-down bonus: deeper drawdowns score higher (within reason)."""
    if dd_pct is None:
        return 50.0
    # dd_pct is negative when below high
    depth = max(0.0, -float(dd_pct))
    if depth < 5:
        return 42.0
    if depth < 15:
        return 55.0
    if depth < 30:
        return 72.0
    if depth < 50:
        return 85.0
    return 78.0  # extreme washouts — slightly less (binary risk)


def reaction_gate(
    *,
    meta: dict[str, Any],
    days: int | None,
    quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Critical post-earnings reaction gate.

    Do NOT auto BUY on the report alone. Prefer WAIT until confirmed reaction
    (open/hold above key level, or AH high/VWAP acceptance).
    """
    status = str(meta.get("status") or STATUS_WATCH)
    reasons: list[str] = []
    accepted = False
    blocked_buy = True

    # Structured ticker rules
    sym = str(meta.get("symbol") or "")
    if sym == "FRMI":
        status = STATUS_WAIT
        reasons.append("FRMI rule: trade confirmed reaction, not the report blindly")
    elif sym == "TSSI":
        status = STATUS_BUY_IF
        reasons.append("TSSI rule: only if AH high/VWAP acceptance after print")
    elif sym == "IREN":
        status = STATUS_WATCH
        reasons.append(
            "IREN rule: only if print confirms AI ARR / contracted revenue ramp "
            "(not sympathy bounce)"
        )

    # Timing: before print → never auto BUY
    if days is not None and days > 0:
        blocked_buy = True
        reasons.append(f"{days}d to earnings — pre-print: no auto BUY")
        if status == STATUS_WATCH and days <= 2:
            status = STATUS_WAIT
            reasons.append("Imminent print → WAIT_FOR_CONFIRMATION")
    elif days is not None and days == 0:
        blocked_buy = True
        reasons.append("Earnings day — WAIT for confirmed reaction (no blind BUY)")
        if status == STATUS_WATCH:
            status = STATUS_WAIT
    elif days is not None and days < 0:
        # Post-print: still require acceptance unless tape already confirms
        reasons.append(f"{abs(days)}d past earnings — reaction gate still on")
        live = None
        if quote:
            live = quote.get("session_change_pct")
            if live is None:
                live = quote.get("change_pct")
            last = quote.get("last")
            vwap = quote.get("vwap") or quote.get("VWAP")
            ah_high = quote.get("ah_high") or quote.get("post_high")
            # Soft acceptance heuristics when live fields exist
            if live is not None and float(live) >= 1.5:
                if vwap is not None and last is not None and float(last) >= float(vwap):
                    accepted = True
                    blocked_buy = False
                    reasons.append("Tape acceptance: green session + hold ≥ VWAP")
                elif ah_high is not None and last is not None and float(last) >= float(ah_high) * 0.995:
                    accepted = True
                    blocked_buy = False
                    reasons.append("Tape acceptance: reclaim/hold AH high")
                else:
                    reasons.append("Green but missing VWAP/AH acceptance — still WAIT")
            else:
                reasons.append("No confirmed acceptance tape yet — WAIT")
        else:
            reasons.append("No live quote for acceptance check — WAIT")

    # Hard: never auto BUY solely from earnings being on calendar
    if not accepted:
        blocked_buy = True

    # Map BUY_ONLY_IF_ACCEPTED when accepted clears
    action = "WAIT"
    if accepted and status == STATUS_BUY_IF:
        action = "BUY_IF_ACCEPTED"
        blocked_buy = False
    elif accepted and status in (STATUS_WAIT, STATUS_WATCH):
        action = "REACTION_OK_REVIEW"
        blocked_buy = False
    elif status == STATUS_BUY_IF:
        action = "BUY_ONLY_IF_ACCEPTED"
    elif status == STATUS_WAIT:
        action = "WAIT_FOR_CONFIRMATION"
    else:
        action = "WATCH"

    return {
        "status": status,
        "action": action,
        "accepted": accepted,
        "blocked_auto_buy": blocked_buy,
        "reasons": reasons,
    }


def score_ml6_name(
    symbol: str,
    df: pd.DataFrame | None,
    *,
    quote: dict[str, Any] | None = None,
    asof: date | None = None,
) -> dict[str, Any]:
    """Combine drawdown + catalyst + theme + liquidity (+ optional vol)."""
    key = str(symbol).replace(".", "-").upper()
    meta = dict(ML6_WATCHLIST.get(key) or {})
    meta["symbol"] = key
    themes = list(meta.get("themes") or [])
    earn = meta.get("earnings_date")
    days = days_to_earnings(earn, asof=asof)

    dd = drawdown_from_high(df) if df is not None else {"drawdown_pct": None, "high": None, "last": None}
    liq = liquidity_metrics(df) if df is not None else {"avg_volume": None, "last_price": None, "passes": False}
    vol = realized_vol_pct(df) if df is not None else None

    t_score = theme_score(themes)
    c_score = catalyst_score(days)
    d_score = drawdown_score(dd.get("drawdown_pct") if isinstance(dd.get("drawdown_pct"), (int, float)) else None)
    # Liquidity: pass → 75, fail hard → 25, soft → 55
    if liq.get("passes"):
        l_score = 75.0
    elif liq.get("last_price") is not None and float(liq["last_price"]) >= MIN_PRICE:
        l_score = 45.0
    else:
        l_score = 25.0

    v_score = 50.0
    if vol is not None:
        # Prefer elevated but not chaos vol for catalyst trades
        if 35 <= vol <= 120:
            v_score = 70.0
        elif vol > 120:
            v_score = 58.0
        else:
            v_score = 48.0

    # Weights: practical blend (no fake ML training)
    ensemble = (
        0.28 * d_score
        + 0.28 * c_score
        + 0.22 * t_score
        + 0.14 * l_score
        + 0.08 * v_score
    )

    gate = reaction_gate(meta=meta, days=days, quote=quote)

    # Hard liquidity gate: mark untradeable but keep on board
    tradeable = bool(liq.get("passes"))
    if not tradeable:
        gate = dict(gate)
        gate["reasons"] = list(gate.get("reasons") or []) + [
            f"Liquidity gate: need price≥{MIN_PRICE} and ~20d avg vol≥{MIN_AVG_VOLUME:,}"
        ]
        # Cap score when illiquid
        ensemble = min(ensemble, 55.0)

    # Never promote auto BUY when gate blocks
    if gate.get("blocked_auto_buy"):
        ensemble = min(ensemble, 89.0)  # keep ranking but UI must not say BUY NOW

    session = meta.get("session") or "est"
    earn_label = f"{earn} {str(session).upper()}" if earn else "—"

    reasons = [
        f"theme={'+'.join(themes) if themes else 'n/a'}",
        f"drawdown={dd.get('drawdown_pct'):.1f}%" if dd.get("drawdown_pct") is not None else "drawdown=n/a",
        f"catalyst_days={days}" if days is not None else "catalyst=n/a",
        f"liq={'ok' if tradeable else 'thin'}",
    ]
    reasons.extend(gate.get("reasons") or [])

    return {
        "symbol": key,
        "horizon": "ml6",
        "name": meta.get("name") or key,
        "earnings_date": earn,
        "earnings_session": session,
        "earnings_label": earn_label,
        "days_to_earnings": days,
        "themes": themes,
        "theme": ", ".join(themes),
        "peer_refs": list(meta.get("peer_refs") or []),
        "status": gate.get("status") or meta.get("status") or STATUS_WATCH,
        "action": gate.get("action"),
        "blocked_auto_buy": bool(gate.get("blocked_auto_buy", True)),
        "accepted": bool(gate.get("accepted")),
        "blurb": meta.get("blurb"),
        "gate": meta.get("gate"),
        "rule_key": meta.get("rule_key"),
        "ensemble_score": round(float(ensemble), 2),
        "score": round(float(ensemble), 2),
        "last_price": dd.get("last") if dd.get("last") is not None else liq.get("last_price"),
        "drawdown_pct": round(float(dd["drawdown_pct"]), 2) if dd.get("drawdown_pct") is not None else None,
        "recent_high": dd.get("high"),
        "avg_volume": liq.get("avg_volume"),
        "realized_vol_pct": round(vol, 1) if vol is not None else None,
        "liquidity_ok": tradeable,
        "components": {
            "drawdown": round(d_score, 1),
            "catalyst": round(c_score, 1),
            "theme": round(t_score, 1),
            "liquidity": round(l_score, 1),
            "volatility": round(v_score, 1),
        },
        "quality": bool(tradeable and ensemble >= 62 and not gate.get("blocked_auto_buy")),
        "confirms": sum(
            1
            for x in (d_score >= 65, c_score >= 65, t_score >= 65, l_score >= 65)
            if x
        ),
        "reasons": reasons,
        "expected_move_pct": None,
        "bullish": ensemble >= 55,
    }
