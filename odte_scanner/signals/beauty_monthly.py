"""Beauty / monthly continuation lane — AMD·META·MU·SNDK-class 1-month plays.

Sep 2026 lesson: these names ripped +15–40% on the month. Desk lived in 0DTE /
1–3d weeklies and booked losers while 20–45 DTE calls would have multi-bagged
(user estimate ~500% on the right 1-month ticket).

This lane surfaces liquid megas/semis still in a monthly uptrend and prefers
~1-month DTE calls (not 0DTE). Separate from hist-gated BUY NOW and RIP (session).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Sep beauty set + peers that print the same monthly melt pattern
BEAUTY_SYMBOLS = {
    "AMD", "META", "MU", "SNDK", "NVDA", "AVGO", "SMCI", "TSM", "ARM", "QCOM",
    "BABA", "GOOGL", "GOOG", "AMZN", "TSLA", "AAPL", "MSFT", "NFLX", "PLTR",
    "ORCL", "CRM", "NOW", "CRWD", "ANET", "ALAB", "VRT", "MRVL",
}


@dataclass
class BeautyAction:
    action: str  # BUY_BEAUTY | WATCH_BEAUTY | BEAUTY_COOL
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "beauty"
    risk_tag: str = "monthly"  # monthly | trend | cool
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    contract: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    dte: int | None = None
    dte_bucket: str = "monthly"
    spot: float | None = None
    moneyness_pct: float | None = None
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    month_change_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    target_premium_mult: float = 3.0  # aim ~+200% (stretch 5× / +400%)
    right: str = "C"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_beauty_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in BEAUTY_SYMBOLS


def _month_change_pct(quote: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> float | None:
    q = quote or {}
    c = candidate or {}
    for k in ("month_change_pct", "1m_change_pct", "change_1m_pct", "perf_1m_pct"):
        if q.get(k) is not None:
            return float(q[k])
        if c.get(k) is not None:
            return float(c[k])
    # Fallback: session as weak proxy when offline Pages has only day tape
    for k in ("session_change_pct", "change_pct", "live_change_pct"):
        if q.get(k) is not None:
            return float(q[k])
        if c.get(k) is not None:
            return float(c[k])
    return None


def beauty_trend_ok(
    *,
    month_pct: float | None,
    live: float | None,
    score: float,
    min_month_pct: float = 5.0,
    min_score: float = 62.0,
) -> bool:
    """True when name is in a constructive monthly grind/rip (not a knife)."""
    if score < min_score and (month_pct is None or month_pct < min_month_pct):
        return False
    if live is not None and live <= -1.5:
        return False  # no beauty buy into a dump day
    if month_pct is not None and month_pct >= min_month_pct:
        return True
    # Strong score + non-negative day can still watch
    return score >= (min_score + 8) and (live is None or live >= -0.35)


def decide_beauty_entry(
    ticket: dict[str, Any],
    *,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    loss_cooldown_contracts: set[str] | None = None,
    min_dte: int = 18,
    max_dte: int = 45,
    prefer_dte: int = 30,
    min_month_pct: float = 5.0,
    min_score: float = 62.0,
    min_ask: float = 0.50,
    max_ask: float = 40.0,
    max_otm_pct: float = 8.0,
    now: datetime | None = None,
) -> BeautyAction:
    symbol = str(ticket.get("symbol") or "").upper()
    contract = str(ticket.get("contract") or "").upper()
    ask = float(ticket.get("ask") or 0)
    bid = float(ticket.get("bid") or 0)
    dte = int(ticket.get("dte") if ticket.get("dte") is not None else -1)
    strike = ticket.get("strike")
    spot = float(ticket.get("spot") or ticket.get("live_spot") or 0)
    if quote and quote.get("last"):
        spot = float(quote["last"]) or spot
    mny = ticket.get("moneyness_pct")
    if mny is None and spot and strike:
        mny = (float(strike) - float(spot)) / float(spot) * 100.0
    live = None
    if quote:
        for k in ("session_change_pct", "change_pct"):
            if quote.get(k) is not None:
                live = float(quote[k])
                break
    if live is None and ticket.get("live_change_pct") is not None:
        live = float(ticket["live_change_pct"])
    month_pct = _month_change_pct(quote, ticket)
    score = float(ensemble_score if ensemble_score is not None else (ticket.get("score") or 0))
    vol = int(ticket.get("volume") or 0)
    oi = int(ticket.get("open_interest") or ticket.get("oi") or 0)
    blocked = {str(c).upper() for c in (loss_cooldown_contracts or set())}

    playbook = [
        "Beauty = 1-month out (≈18–45 DTE) on liquid mega/semi trends — not 0DTE lottery",
        "Sep pattern: AMD/META/MU/SNDK +15–40% stock → monthly calls multi-bag",
        "Target bank +100–400% premium (3–5×); trail after +100%",
        "Same losing OCC stays blocked; size ≤35% cash",
    ]
    base = dict(
        symbol=symbol,
        contract=contract or None,
        strike=strike,
        expiry=ticket.get("expiry"),
        ask=ask or None,
        bid=bid or None,
        dte=dte if dte >= 0 else None,
        spot=spot or None,
        moneyness_pct=float(mny) if mny is not None else None,
        ensemble_score=score,
        live_change_pct=live,
        month_change_pct=month_pct,
        volume=vol or None,
        open_interest=oi or None,
        right="C",
    )

    if not is_beauty_symbol(symbol):
        return BeautyAction(
            action="BEAUTY_COOL",
            strength=20.0,
            headline=f"BEAUTY COOL {symbol}",
            detail="Not in beauty sleeve (AMD/META/MU/SNDK + peers).",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if contract and contract in blocked:
        return BeautyAction(
            action="BEAUTY_COOL",
            strength=25.0,
            headline=f"BEAUTY COOL {symbol}",
            detail=f"Contract {contract} on loss cooldown — roll to a fresh monthly strike.",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    # DTE gate: beauty wants ~1 month, not 0DTE
    dte_ok = min_dte <= dte <= max_dte
    near_monthly = dte >= 10  # soft watch if at least 10 DTE

    if ask <= 0:
        trend = beauty_trend_ok(month_pct=month_pct, live=live, score=score, min_month_pct=min_month_pct, min_score=min_score)
        if trend:
            return BeautyAction(
                action="WATCH_BEAUTY",
                strength=min(70.0, 40 + score * 0.3 + (month_pct or 0)),
                headline=f"WATCH BEAUTY {symbol}",
                detail=(
                    f"Trend warm"
                    + (f" (month {month_pct:+.1f}%)" if month_pct is not None else "")
                    + f" — pull ~{prefer_dte}DTE near-ATM call (no ask on snapshot)."
                ),
                playbook=playbook,
                risk_tag="trend",
                confirms=2,
                **base,
            )
        return BeautyAction(
            action="BEAUTY_COOL",
            strength=max(0.0, score * 0.4),
            headline=f"BEAUTY COOL {symbol}",
            detail="No monthly ask yet / trend not confirmed.",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if ask < min_ask or ask > max_ask:
        return BeautyAction(
            action="BEAUTY_COOL",
            strength=30.0,
            headline=f"BEAUTY COOL {symbol}",
            detail=f"Ask ${ask:.2f} outside beauty band ${min_ask:.2f}–${max_ask:.2f}.",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if mny is not None and float(mny) > max_otm_pct:
        return BeautyAction(
            action="WATCH_BEAUTY",
            strength=55.0,
            headline=f"WATCH BEAUTY {symbol}",
            detail=f"Too far OTM ({float(mny):.1f}%) for beauty — prefer ≤{max_otm_pct:.0f}% OTM monthly.",
            playbook=playbook,
            risk_tag="monthly",
            confirms=1,
            **base,
        )

    confirms = 0
    if is_beauty_symbol(symbol):
        confirms += 1
    if score >= min_score:
        confirms += 1
    if month_pct is not None and month_pct >= min_month_pct:
        confirms += 2
    elif month_pct is not None and month_pct >= min_month_pct * 0.5:
        confirms += 1
    if live is not None and live >= 0:
        confirms += 1
    if dte_ok:
        confirms += 2
    elif near_monthly:
        confirms += 1
    if vol >= 50 or oi >= 200:
        confirms += 1

    dte_fit = 1.0 - min(1.0, abs(dte - prefer_dte) / max(prefer_dte, 1))
    strength = min(
        100.0,
        25
        + score * 0.35
        + (month_pct or 0) * 1.2
        + max(0.0, live or 0) * 4
        + dte_fit * 15
        + confirms * 3,
    )

    trend = beauty_trend_ok(
        month_pct=month_pct, live=live, score=score, min_month_pct=min_month_pct, min_score=min_score
    )

    if trend and dte_ok and confirms >= 4:
        return BeautyAction(
            action="BUY_BEAUTY",
            strength=strength,
            headline=f"BUY BEAUTY {symbol} · ~1mo",
            detail=(
                f"Monthly beauty: DTE {dte}"
                + (f", month {month_pct:+.1f}%" if month_pct is not None else "")
                + (f", day {live:+.2f}%" if live is not None else "")
                + f" · call @ ${ask:.2f} · target 3–5× (Sep AMD/META pattern)"
            ),
            playbook=playbook,
            risk_tag="monthly",
            confirms=confirms,
            target_premium_mult=3.0,
            **base,
        )

    if trend and (dte_ok or near_monthly):
        return BeautyAction(
            action="WATCH_BEAUTY",
            strength=min(strength, 75.0),
            headline=f"WATCH BEAUTY {symbol}",
            detail=(
                f"Trend OK but need clearer monthly ticket "
                f"(DTE now {dte if dte >= 0 else '—'}, want {min_dte}–{max_dte})."
            ),
            playbook=playbook,
            risk_tag="trend",
            confirms=confirms,
            **base,
        )

    return BeautyAction(
        action="BEAUTY_COOL",
        strength=max(0.0, strength * 0.5),
        headline=f"BEAUTY COOL {symbol}",
        detail=(
            f"No beauty setup yet"
            + (f" (month {month_pct:+.1f}%)" if month_pct is not None else "")
            + f" · DTE {dte if dte >= 0 else '—'}."
        ),
        playbook=playbook,
        risk_tag="cool",
        confirms=confirms,
        **base,
    )


def days_to_deadline(deadline: str | date | None = "2026-10-31", *, now: datetime | None = None) -> int:
    now = now or datetime.now(ET)
    if deadline is None:
        return 30
    if isinstance(deadline, str):
        d = date.fromisoformat(deadline[:10])
    else:
        d = deadline
    return max(1, (d - now.date()).days)


def oct_end_pace_note(
    *,
    equity: float = 1000.0,
    target_usd: float = 1_000_000.0,
    deadline: str = "2026-10-31",
    ideal_hold_days: float = 2.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Math for $1k→$1M by Oct-end given current equity."""
    days = days_to_deadline(deadline, now=now)
    flips = max(1, int(days // float(ideal_hold_days)))
    need = float(target_usd) / max(float(equity), 1.0)
    mult = need ** (1.0 / flips) if flips else need
    pct = (mult - 1.0) * 100.0
    return {
        "deadline": deadline,
        "days_left": days,
        "flips_in_window": flips,
        "ideal_hold_days": ideal_hold_days,
        "current_equity": round(float(equity), 2),
        "target_usd": float(target_usd),
        "mult_per_flip": round(mult, 4),
        "pct_per_flip": round(pct, 1),
        "feasible": pct <= 250.0,
        "note": (
            f"Oct-end ({deadline}) pace: {days}d left → ~{flips} flips @ ~{ideal_hold_days:g}d "
            f"need ~+{pct:.0f}%/flip from ${equity:,.0f} → ${target_usd:,.0f}."
        ),
    }


def build_beauty_board(
    *,
    candidates: list[dict[str, Any]],
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    loss_cooldown_contracts: set[str] | list[str] | None = None,
    min_dte: int = 18,
    max_dte: int = 45,
    prefer_dte: int = 30,
    min_month_pct: float = 5.0,
    max_tickets: int = 12,
    now: datetime | None = None,
) -> dict[str, Any]:
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol") or "").upper(): float(s.get("ensemble_score") or 0)
        for s in (scores or [])
    }
    blocked = {str(c).upper() for c in (loss_cooldown_contracts or [])}
    buys: list[BeautyAction] = []
    watches: list[BeautyAction] = []
    cool: list[BeautyAction] = []
    seen: set[str] = set()

    ranked = sorted(
        candidates or [],
        key=lambda c: (
            0 if is_beauty_symbol(str(c.get("symbol") or "")) else 1,
            # Prefer monthly DTE when present
            0 if min_dte <= int(c.get("dte") or -1) <= max_dte else 1,
            -float(c.get("score") or score_map.get(str(c.get("symbol") or "").upper(), 0)),
        ),
    )
    for c in ranked:
        sym = str(c.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        if str(c.get("right") or "C").upper() == "P":
            continue
        seen.add(sym)
        act = decide_beauty_entry(
            c,
            quote=quotes.get(sym),
            ensemble_score=score_map.get(sym) or c.get("score"),
            loss_cooldown_contracts=blocked,
            min_dte=min_dte,
            max_dte=max_dte,
            prefer_dte=prefer_dte,
            min_month_pct=min_month_pct,
            now=now,
        )
        if act.action == "BUY_BEAUTY":
            buys.append(act)
        elif act.action == "WATCH_BEAUTY":
            watches.append(act)
        else:
            cool.append(act)

    buys.sort(key=lambda a: a.strength, reverse=True)
    watches.sort(key=lambda a: a.strength, reverse=True)
    primary = buys[0] if buys else (watches[0] if watches else None)
    pace = oct_end_pace_note(now=now)
    return {
        "label": "BEAUTY / MONTHLY",
        "purpose": (
            "Catch AMD·META·MU·SNDK-class monthly melts with ~18–45 DTE calls. "
            "0DTE/short weeklies missed Sep 2026 multi-baggers — this lane prefers 1-month out."
        ),
        "lesson": (
            "Sep 2026: AMD ~+34% / META ~+28% / MU ~+12% / SNDK ~+15% month-to-date; "
            "low→high up to +40%. 1-month ATM/slight OTM calls → multi-bag potential (~200–500%+)."
        ),
        "primary": primary.to_dict() if primary else None,
        "buy_beauty": [a.to_dict() for a in buys[:max_tickets]],
        "buy_now": [a.to_dict() for a in buys[:max_tickets]],
        "watch": [a.to_dict() for a in watches[:max_tickets]],
        "cool": [a.to_dict() for a in cool[:max_tickets]],
        "all": [a.to_dict() for a in (buys + watches + cool)[: max_tickets * 2]],
        "counts": {
            "buy_beauty": len(buys),
            "buy_now": len(buys),
            "watch": len(watches),
            "cool": len(cool),
        },
        "beauty_symbols": sorted(BEAUTY_SYMBOLS),
        "dte_band": {"min": min_dte, "max": max_dte, "prefer": prefer_dte},
        "oct_end_pace": pace,
        "rules": [
            f"Prefer DTE {min_dte}–{max_dte} (target ~{prefer_dte}d) — skip pure 0DTE on this lane",
            "Names: AMD META MU SNDK + liquid mega/semi peers",
            "Trend: month ≥~5% or strong score; no dump-day entries",
            "Target bank 3–5× premium; same OCC loss cooldown still applies",
            pace["note"],
        ],
        "generated_at": (now or datetime.now(ET)).isoformat(),
    }
