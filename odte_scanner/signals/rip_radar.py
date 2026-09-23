"""META-class RIP / CONTINUATION lane — liquid megas ripping on tape.

Separate from gated Options BUY NOW (hist ≥80% n≥5) and from far-OTM chase.

Surfaces names like META/GOOGL/AMD/BABA when session + short-term momentum
confirm a continuation. Still never rebuys the same losing OCC contract.
Symbol loss-cooldown is waived here when the tape is ripping — that is what
blocked META before yesterday's melt-up.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Liquid megas / China ADRs / semis the desk wants on RIP alerts
MEGA_RIP_SYMBOLS = {
    "META", "GOOGL", "GOOG", "AMD", "BABA", "NVDA", "TSLA", "AAPL", "AMZN",
    "MSFT", "NFLX", "AVGO", "MU", "SMCI", "PLTR", "TSM", "QCOM", "ARM",
    "SPOT", "SHOP", "CRWD", "PANW", "ORCL", "IBM", "INTC", "SNDK",
}


@dataclass
class RipAction:
    action: str  # BUY_RIP | WATCH_RIP | RIP_COOL
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "rip"
    risk_tag: str = "continuation"  # continuation | mega_rip | cool
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    contract: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    dte: int | None = None
    dte_bucket: str | None = None
    spot: float | None = None
    moneyness_pct: float | None = None
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    mom_5m_pct: float | None = None
    mom_15m_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    win_pct: float | None = None
    win_samples: int | None = None
    cooldown_waived: bool = False
    right: str = "C"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _live_pct(quote: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> float | None:
    if quote:
        for k in ("session_change_pct", "change_pct", "live_change_pct"):
            if quote.get(k) is not None:
                return float(quote[k])
    if candidate and candidate.get("live_change_pct") is not None:
        return float(candidate["live_change_pct"])
    return None


def is_mega_rip_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in MEGA_RIP_SYMBOLS


def mega_rip_tape_ok(
    *,
    live: float | None,
    mom5: float | None,
    mom15: float | None = None,
    min_live_pct: float = 1.0,
    min_mom5: float = 0.05,
) -> bool:
    """True when underlying is in a META-class rip (session + bounce)."""
    if live is None or live < float(min_live_pct):
        return False
    if mom5 is not None and mom5 < float(min_mom5):
        return False
    if mom5 is None and (mom15 is None or mom15 < float(min_mom5)):
        return False
    return True


def decide_rip_entry(
    ticket: dict[str, Any],
    *,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    loss_cooldown_contracts: set[str] | None = None,
    min_live_pct: float = 1.0,
    min_mom5: float = 0.05,
    min_ask: float = 0.25,
    max_ask: float = 25.0,
    max_otm_pct: float = 4.0,
    min_volume: int = 50,
    now: datetime | None = None,
) -> RipAction:
    """Classify a mega call as BUY_RIP / WATCH_RIP / RIP_COOL."""
    now = now or datetime.now(ET)
    symbol = str(ticket.get("symbol") or "").upper()
    contract = str(ticket.get("contract") or "").upper()
    ask = float(ticket.get("ask") or 0)
    bid = float(ticket.get("bid") or 0)
    dte = int(ticket.get("dte") if ticket.get("dte") is not None else 99)
    strike = ticket.get("strike")
    spot = float(ticket.get("spot") or ticket.get("live_spot") or 0)
    if quote and quote.get("last"):
        spot = float(quote["last"]) or spot
    mny = ticket.get("moneyness_pct")
    if mny is None and spot and strike:
        mny = (float(strike) - float(spot)) / float(spot) * 100.0
    live = _live_pct(quote, ticket)
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    vol = int(ticket.get("volume") or 0)
    oi = int(ticket.get("open_interest") or ticket.get("oi") or 0)
    score = float(ensemble_score if ensemble_score is not None else (ticket.get("score") or 0))
    blocked_ct = {str(c).upper() for c in (loss_cooldown_contracts or set())}

    base = dict(
        symbol=symbol,
        contract=contract or None,
        strike=strike,
        expiry=ticket.get("expiry"),
        ask=ask or None,
        bid=bid or None,
        dte=dte,
        dte_bucket=ticket.get("dte_bucket") or ("0dte" if dte <= 1 else "weekly"),
        spot=spot or None,
        moneyness_pct=float(mny) if mny is not None else None,
        ensemble_score=score,
        live_change_pct=live,
        mom_5m_pct=mom5,
        mom_15m_pct=mom15,
        volume=vol or None,
        open_interest=oi or None,
        win_pct=ticket.get("win_pct"),
        win_samples=ticket.get("win_samples"),
        right="C",
    )

    playbook = [
        "META-class continuation: liquid mega + session rip + short-term bounce",
        "Never rebuy the same losing OCC contract",
        "Size small vs gated hist BUY NOW — premium can still go to zero",
        "Exit: bank +50–80% or trail; cut −35–45%",
    ]

    if not is_mega_rip_symbol(symbol):
        return RipAction(
            action="RIP_COOL",
            strength=max(0.0, score * 0.4),
            headline=f"RIP COOL {symbol}",
            detail="Not in mega/continuation sleeve (META/GOOGL/AMD/BABA/…).",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if contract and contract in blocked_ct:
        return RipAction(
            action="RIP_COOL",
            strength=30.0,
            headline=f"RIP COOL {symbol}",
            detail=f"Contract {contract} on loss cooldown — pick a fresh strike/expiry.",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if ask <= 0 or ask < min_ask or ask > max_ask:
        if mega_rip_tape_ok(live=live, mom5=mom5, mom15=mom15, min_live_pct=min_live_pct, min_mom5=min_mom5):
            return RipAction(
                action="WATCH_RIP",
                strength=min(70.0, 45 + (live or 0) * 6),
                headline=f"WATCH RIP {symbol}",
                detail=(
                    f"Mega ripping (session {live:+.2f}%) but no liquid call ask on this snapshot — pull a near-ATM weekly."
                    if live is not None
                    else "Mega tape warm but no liquid call ask on this snapshot."
                ),
                playbook=playbook,
                risk_tag="continuation",
                confirms=2,
                **base,
            )
        return RipAction(
            action="RIP_COOL",
            strength=max(0.0, score * 0.5),
            headline=f"RIP COOL {symbol}",
            detail=f"Ask ${ask:.2f} outside rip band ${min_ask:.2f}–${max_ask:.2f}.",
            playbook=playbook,
            risk_tag="cool",
            **base,
        )

    if mny is not None and float(mny) > max_otm_pct:
        return RipAction(
            action="WATCH_RIP",
            strength=min(70.0, 40 + score * 0.3),
            headline=f"WATCH RIP {symbol}",
            detail=f"Too far OTM ({float(mny):.1f}% > {max_otm_pct:.0f}%) — prefer nearer strikes on the rip.",
            playbook=playbook,
            risk_tag="continuation",
            confirms=1,
            **base,
        )

    ripping = mega_rip_tape_ok(
        live=live, mom5=mom5, mom15=mom15, min_live_pct=min_live_pct, min_mom5=min_mom5
    )
    # Offline Pages: session % alone can clear a softer watch
    offline_soft = live is not None and live >= min_live_pct and mom5 is None and mom15 is None

    confirms = 0
    if live is not None and live >= min_live_pct:
        confirms += 1
    if mom5 is not None and mom5 >= min_mom5:
        confirms += 1
    if mom15 is not None and mom15 >= 0:
        confirms += 1
    if score >= 65:
        confirms += 1
    if vol >= min_volume or oi >= 200:
        confirms += 1

    strength = min(
        100.0,
        35
        + (live or 0) * 8
        + (mom5 or 0) * 20
        + max(0.0, score - 60) * 0.8
        + confirms * 4,
    )

    if ripping and confirms >= 3 and (vol >= min_volume or oi >= 100 or ask > 0):
        return RipAction(
            action="BUY_RIP",
            strength=strength,
            headline=f"BUY RIP {symbol} · continuation",
            detail=(
                f"Mega rip: session {live:+.2f}%"
                + (f", 5m {mom5:+.2f}%" if mom5 is not None else "")
                + f" · {base['dte_bucket']} call @ ${ask:.2f}"
                + (f" · {float(mny):.1f}% OTM" if mny is not None else "")
                + " · META-class continuation (symbol cooldown waived; OCC still blocked)"
            ),
            playbook=playbook,
            risk_tag="mega_rip",
            confirms=confirms,
            cooldown_waived=True,
            **base,
        )

    if ripping or offline_soft or (live is not None and live >= min_live_pct * 0.7):
        return RipAction(
            action="WATCH_RIP",
            strength=min(strength, 72.0),
            headline=f"WATCH RIP {symbol}",
            detail=(
                f"Tape warming (session {live:+.2f}%) — need bounce confirm / liquidity for BUY RIP."
                if live is not None
                else "Tape warming — need bounce confirm / liquidity for BUY RIP."
            ),
            playbook=playbook,
            risk_tag="continuation",
            confirms=confirms,
            **base,
        )

    return RipAction(
        action="RIP_COOL",
        strength=max(0.0, strength * 0.5),
        headline=f"RIP COOL {symbol}",
        detail=(
            f"No rip yet (session {live:+.2f}%, need ≥{min_live_pct:.1f}% + bounce)."
            if live is not None
            else f"No rip yet (need ≥{min_live_pct:.1f}% session + bounce)."
        ),
        playbook=playbook,
        risk_tag="cool",
        confirms=confirms,
        **base,
    )


def build_rip_board(
    *,
    candidates: list[dict[str, Any]],
    scores: list[dict[str, Any]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    loss_cooldown_contracts: set[str] | list[str] | None = None,
    min_live_pct: float = 1.0,
    min_mom5: float = 0.05,
    max_tickets: int = 12,
    now: datetime | None = None,
) -> dict[str, Any]:
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol") or "").upper(): float(s.get("ensemble_score") or 0)
        for s in (scores or [])
    }
    blocked = {str(c).upper() for c in (loss_cooldown_contracts or [])}
    buys: list[RipAction] = []
    watches: list[RipAction] = []
    cool: list[RipAction] = []
    seen: set[str] = set()

    # Prefer megas; also scan any candidate already on the options board
    ranked = sorted(
        candidates or [],
        key=lambda c: (
            0 if is_mega_rip_symbol(str(c.get("symbol") or "")) else 1,
            -float(c.get("score") or score_map.get(str(c.get("symbol") or "").upper(), 0)),
        ),
    )
    for c in ranked:
        sym = str(c.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        # One ticket per symbol on this lane
        if str(c.get("right") or "C").upper() == "P":
            continue
        seen.add(sym)
        act = decide_rip_entry(
            c,
            quote=quotes.get(sym),
            ensemble_score=score_map.get(sym) or c.get("score"),
            loss_cooldown_contracts=blocked,
            min_live_pct=min_live_pct,
            min_mom5=min_mom5,
            now=now,
        )
        if act.action == "BUY_RIP":
            buys.append(act)
        elif act.action == "WATCH_RIP":
            watches.append(act)
        else:
            cool.append(act)

    buys.sort(key=lambda a: a.strength, reverse=True)
    watches.sort(key=lambda a: a.strength, reverse=True)
    primary = buys[0] if buys else (watches[0] if watches else None)
    return {
        "label": "RIP / CONTINUATION",
        "purpose": (
            "META-class liquid megas (META, GOOGL, AMD, BABA, NVDA, …) when session "
            "is ripping and tape confirms. Waives symbol loss-cooldown; never rebuys "
            "the same losing OCC. Not hist-gated — size smaller than Options BUY NOW."
        ),
        "primary": primary.to_dict() if primary else None,
        "buy_now": [a.to_dict() for a in buys[:max_tickets]],
        "buy_rip": [a.to_dict() for a in buys[:max_tickets]],
        "watch": [a.to_dict() for a in watches[:max_tickets]],
        "cool": [a.to_dict() for a in cool[:max_tickets]],
        "all": [a.to_dict() for a in (buys + watches + cool)[: max_tickets * 2]],
        "counts": {
            "buy_rip": len(buys),
            "buy_now": len(buys),
            "watch": len(watches),
            "cool": len(cool),
        },
        "mega_symbols": sorted(MEGA_RIP_SYMBOLS),
        "rules": [
            "Need session ≥~1% and short-term bounce (or soft offline session rip).",
            "Prefer ATM–near OTM (≤4%) liquid calls on focus megas.",
            "Same OCC after a loss stays blocked ~45d.",
            "Symbol cooldown waived on this lane when tape is ripping.",
            "Research / discretionary — not auto-journal gated BUY NOW.",
        ],
        "generated_at": (now or datetime.now(ET)).isoformat(),
    }
