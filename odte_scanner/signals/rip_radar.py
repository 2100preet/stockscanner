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

from odte_scanner.time_cst import (
    append_asked_cst,
    load_signal_store,
    prune_signal_store_to_active,
    resolve_first_signal_time,
    save_signal_store,
    signal_store_key,
    signal_timestamps,
)

ET = ZoneInfo("America/New_York")

# Desk special-eye — always seed RIP / early options / UW focus.
# Standing megas + session catalyst / premarket movers (merged dynamically too).
DESK_SPECIAL_EYE: frozenset[str] = frozenset(
    {
        "HOOD",
        "MSFT",
        "INTC",
        "PLTR",
        "AMZN",
        "MU",
        "AVGO",
        "GOOGL",
        "GOOG",
        "AMD",
        # 2026-10-09 catalysts — SpaceX spectrum / MA stars / AAPL cut / AI rebound
        "TMUS",
        "T",
        "VZ",
        "CCI",
        "AMT",
        "SBAC",
        "SPCX",
        "HUM",
        "ALHC",
        "AAPL",
        "ORCL",
        "CRWV",
        "NVDA",
        "LITE",
    }
)

# Liquid megas / China ADRs / semis the desk wants on RIP alerts
MEGA_RIP_SYMBOLS = {
    "META", "GOOGL", "GOOG", "AMD", "BABA", "NVDA", "TSLA", "AAPL", "AMZN",
    "MSFT", "NFLX", "AVGO", "MU", "SMCI", "PLTR", "TSM", "QCOM", "ARM",
    "SPOT", "SHOP", "CRWD", "PANW", "ORCL", "IBM", "INTC", "SNDK",
} | set(DESK_SPECIAL_EYE)


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
    signaled_at: str | None = None
    signaled_at_cst: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.action == "BUY_RIP" and not d.get("signaled_at"):
            d.update(signal_timestamps())
        return d


def _apply_persisted_rip(
    sig: "RipAction",
    store: dict[str, Any],
) -> tuple["RipAction", dict[str, Any]]:
    if sig.action != "BUY_RIP":
        return sig, store
    utc, cst, store = resolve_first_signal_time(
        store,
        symbol=sig.symbol,
        action=sig.action,
        contract=getattr(sig, "contract", None),
    )
    sig.signaled_at = utc
    sig.signaled_at_cst = cst
    sig.detail = append_asked_cst(sig.detail, action=sig.action, signaled_at_cst=cst)
    return sig, store


def _live_pct(quote: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> float | None:
    if quote:
        for k in ("session_change_pct", "change_pct", "live_change_pct", "day_change_pct"):
            v = quote.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    if candidate:
        for k in ("live_change_pct", "session_change_pct", "change_pct"):
            v = candidate.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


def is_mega_rip_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in MEGA_RIP_SYMBOLS


def bounce_from_day_low_pct(quote: dict[str, Any] | None, spot: float | None = None) -> float | None:
    """Percent reclaim off the day low — catches TSLA 346→351 while still red vs prior close."""
    if not quote:
        return None
    low = quote.get("day_low")
    last = spot
    if last is None:
        last = quote.get("last")
    if low is None or last is None:
        return None
    try:
        low_f = float(low)
        last_f = float(last)
    except (TypeError, ValueError):
        return None
    if low_f <= 0:
        return None
    return (last_f / low_f - 1.0) * 100.0


def mega_rip_tape_ok(
    *,
    live: float | None,
    mom5: float | None,
    mom15: float | None = None,
    min_live_pct: float = 1.0,
    min_mom5: float = 0.05,
    bounce_from_low: float | None = None,
    min_bounce_from_low: float = 1.2,
) -> bool:
    """True when underlying is in a META-class rip (session + bounce).

    Pages offline often lacks 5m/15m bars — session ≥ min alone still counts
    as a rip so INTC/GOOGL-class +2–3% days are not stuck on RIP_COOL.

    Also treats a ≥min_bounce_from_low reclaim off the day low as a rip even when
    session % vs prior close is still flat/red (TSLA 347→351 case).
    """
    reclaim = (
        bounce_from_low is not None and float(bounce_from_low) >= float(min_bounce_from_low)
    )
    session_rip = live is not None and live >= float(min_live_pct)
    if not session_rip and not reclaim:
        return False
    if mom5 is None and mom15 is None:
        return True
    if mom5 is not None and mom5 < float(min_mom5):
        # Allow day-low reclaim through a soft 5m dip (washout bounce)
        if reclaim and mom5 >= -0.15:
            return True
        return False
    if mom5 is None and mom15 is not None and mom15 < float(min_mom5):
        if reclaim and mom15 >= -0.20:
            return True
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
    bounce_low = bounce_from_day_low_pct(quote, spot or None)
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
        "META-class continuation: liquid mega + session rip / day-low reclaim",
        "Never rebuy the same losing OCC contract",
        "Size small vs gated hist BUY NOW — premium can still go to zero",
        "Exit: bank +50–80% or trail; cut −35–45%",
    ]

    def _tape_ok() -> bool:
        return mega_rip_tape_ok(
            live=live,
            mom5=mom5,
            mom15=mom15,
            min_live_pct=min_live_pct,
            min_mom5=min_mom5,
            bounce_from_low=bounce_low,
        )

    def _tape_label() -> str:
        bits = []
        if live is not None:
            bits.append(f"session {live:+.2f}%")
        if bounce_low is not None:
            bits.append(f"off day-low +{bounce_low:.2f}%")
        return ", ".join(bits) if bits else "tape"

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
        if _tape_ok():
            return RipAction(
                action="WATCH_RIP",
                strength=min(70.0, 45 + (live or 0) * 6 + (bounce_low or 0) * 4),
                headline=f"WATCH RIP {symbol}",
                detail=(
                    f"Mega reclaiming ({_tape_label()}) but no liquid call ask on this snapshot — pull a near-ATM weekly."
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

    ripping = _tape_ok()
    # Offline Pages: session % alone can clear a softer watch
    offline_soft = live is not None and live >= min_live_pct and mom5 is None and mom15 is None
    reclaim_soft = bounce_low is not None and bounce_low >= 1.2

    confirms = 0
    if live is not None and live >= min_live_pct:
        confirms += 1
        # Pages often has no 5m/15m — count session rip as bounce proxy once
        if mom5 is None and mom15 is None:
            confirms += 1
    if reclaim_soft:
        confirms += 1
        if mom5 is None and mom15 is None and (live is None or live < min_live_pct):
            confirms += 1  # day-low reclaim stands in for session when still red vs prior
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
        + (bounce_low or 0) * 3
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
                f"Mega rip: {_tape_label()}"
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

    if ripping or offline_soft or reclaim_soft or (live is not None and live >= min_live_pct * 0.7):
        return RipAction(
            action="WATCH_RIP",
            strength=min(strength, 72.0),
            headline=f"WATCH RIP {symbol}",
            detail=(
                f"Tape warming ({_tape_label()}) — need bounce confirm / liquidity for BUY RIP."
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
            f"No rip yet ({_tape_label() or 'flat'}; need ≥{min_live_pct:.1f}% session or ≥1.2% off day-low)."
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
    signal_times_path: str | None = "outputs/rip_signal_times.json",
) -> dict[str, Any]:
    quotes = quotes or {}
    store = load_signal_store(signal_times_path)
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
        q = quotes.get(sym) or {}
        # Stamp tape onto the ticket so decide_rip_entry still works when the
        # quotes map missed a mega (Pages only refreshes a short quote_syms set).
        if c.get("live_change_pct") is None:
            live_pct = q.get("session_change_pct")
            if live_pct is None:
                live_pct = q.get("change_pct")
            if live_pct is not None:
                c = {**c, "live_change_pct": live_pct}
        act = decide_rip_entry(
            c,
            quote=q or None,
            ensemble_score=score_map.get(sym) or c.get("score"),
            loss_cooldown_contracts=blocked,
            min_live_pct=min_live_pct,
            min_mom5=min_mom5,
            now=now,
        )
        if act.action == "BUY_RIP":
            act, store = _apply_persisted_rip(act, store)
            buys.append(act)
        elif act.action == "WATCH_RIP":
            watches.append(act)
        else:
            cool.append(act)

    buys.sort(key=lambda a: a.strength, reverse=True)
    watches.sort(key=lambda a: a.strength, reverse=True)
    active_keys = {
        signal_store_key(a.symbol, a.action, getattr(a, "contract", None))
        for a in buys
    }
    store = prune_signal_store_to_active(store, active_keys)
    save_signal_store(signal_times_path, store)
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
        "signal_times": store,
        "rules": [
            "Need session ≥~1% OR ≥1.2% reclaim off the day low (TSLA 347→351 style).",
            "Prefer ATM–near OTM (≤4%) liquid calls on focus megas.",
            "Same OCC after a loss stays blocked ~45d.",
            "Symbol cooldown waived on this lane when tape is ripping.",
            "Research / discretionary — not auto-journal gated BUY NOW.",
        ],
        "generated_at": (now or datetime.now(ET)).isoformat(),
    }
