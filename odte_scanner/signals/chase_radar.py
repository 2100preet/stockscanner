"""High-convexity / chase-aware alert lane — separate from gated BUY NOW.

Surfaces far-OTM / already-ripping short-dated calls that the main desk
intentionally skips (anti-chase, hist-win, tight ATM band). Labels them
BUY_RISKY / WATCH_CONVEX — discretionary size-small, not MUST TRADE.

Does NOT feed paper journal auto-enter and does NOT bypass Options hist-win.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery import _live_pct, _session_phase

ET = ZoneInfo("America/New_York")


@dataclass
class ChaseAction:
    action: str  # BUY_RISKY | WATCH_CONVEX | CHASE_COOL
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "chase"
    risk_tag: str = "chase"  # chase | far_otm | extended | cool
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    vetoes: list[str] = field(default_factory=list)
    contract: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    dte: int | None = None
    spot: float | None = None
    moneyness_pct: float | None = None
    lottery_score: float | None = None
    best_mult: float | None = None
    mult_at_1pct: float | None = None
    mult_at_2pct: float | None = None
    mult_at_3pct: float | None = None
    mult_at_5pct: float | None = None
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    mom_5m_pct: float | None = None
    mom_15m_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_chase_entry(
    ticket: dict[str, Any],
    *,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    min_ask: float = 0.20,
    max_ask: float = 12.0,
    max_otm_pct: float = 8.0,
    min_mult_at_3pct: float = 3.5,
    min_mom_5m: float = 0.08,
    now: datetime | None = None,
) -> ChaseAction:
    """Classify a convex wing as BUY_RISKY / WATCH_CONVEX / CHASE_COOL."""
    symbol = str(ticket.get("symbol") or "")
    contract = str(ticket.get("contract") or "")
    ask = float(ticket.get("ask") or 0)
    bid = float(ticket.get("bid") or 0)
    dte = int(ticket.get("dte") if ticket.get("dte") is not None else 99)
    strike = ticket.get("strike")
    spot = float(ticket.get("spot") or ticket.get("live_spot") or 0)
    if quote and quote.get("last"):
        spot = float(quote["last"]) or spot
    mny = ticket.get("moneyness_pct")
    if mny is None and spot > 0 and strike is not None:
        mny = (float(strike) - spot) / spot * 100.0
    mny = float(mny or 0)
    ens = float(
        ensemble_score
        if ensemble_score is not None
        else ticket.get("score") or ticket.get("ensemble_score") or 0
    )
    lottery_score = float(ticket.get("lottery_score") or 0)
    mult1 = float(ticket.get("mult_at_1pct") or 0)
    mult2 = float(ticket.get("mult_at_2pct") or 0)
    mult3 = float(ticket.get("mult_at_3pct") or 0)
    mult5 = float(ticket.get("mult_at_5pct") or 0)
    best_mult = float(ticket.get("best_mult") or max(mult1, mult2, mult3, mult5, 0))
    vol = int(ticket.get("volume") or 0)
    oi = int(ticket.get("open_interest") or 0)

    live = _live_pct(quote)
    if live is None and ticket.get("live_change_pct") is not None:
        live = float(ticket["live_change_pct"])
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    phase = _session_phase(now)

    base = dict(
        symbol=symbol,
        contract=contract or None,
        strike=float(strike) if strike is not None else None,
        expiry=ticket.get("expiry"),
        ask=ask or None,
        bid=bid or None,
        dte=dte,
        spot=spot or None,
        moneyness_pct=round(mny, 3),
        lottery_score=lottery_score or None,
        best_mult=best_mult or None,
        mult_at_1pct=mult1 or None,
        mult_at_2pct=mult2 or None,
        mult_at_3pct=mult3 or None,
        mult_at_5pct=mult5 or None,
        ensemble_score=ens or None,
        live_change_pct=live,
        mom_5m_pct=mom5,
        mom_15m_pct=mom15,
        volume=vol,
        open_interest=oi,
    )

    vetoes: list[str] = []
    if dte > 1:
        vetoes.append("DTE>1 — chase lane is 0DTE/1DTE only")
    if ask <= 0:
        vetoes.append("no ask")
    if ask < min_ask:
        vetoes.append(f"ask ${ask:.2f} below chase floor ${min_ask:.2f}")
    if ask > max_ask:
        vetoes.append(f"ask ${ask:.2f} above chase ceiling ${max_ask:.2f} — premium already rich")
    if mny > max_otm_pct:
        vetoes.append(f"too far OTM ({mny:+.2f}% > {max_otm_pct:.2f}%)")
    if mny < -1.5:
        vetoes.append(f"deep ITM ({mny:+.2f}%) — not a convex chase wing")
    if mult3 < min_mult_at_3pct and best_mult < 5.0 and mult5 < 6.0:
        vetoes.append(f"weak convexity (3%→{mult3:.1f}× / best {best_mult:.1f}×)")
    if phase in {"weekend", "final_30"}:
        vetoes.append(f"session phase {phase} — no new chase alerts")
    if mom5 is not None and mom5 <= -0.20:
        vetoes.append(f"5m dump {mom5:+.2f}% — knife (chase lane still refuses dumps)")
    if live is not None and live <= -1.5:
        vetoes.append(f"session soft {live:+.2f}%")

    if vetoes:
        return ChaseAction(
            action="CHASE_COOL",
            strength=max(0.0, lottery_score * 0.3),
            headline=f"CHASE COOL {symbol}",
            detail="; ".join(vetoes[:3]),
            risk_tag="cool",
            playbook=["chase_filter"],
            confirms=0,
            vetoes=vetoes,
            **base,
        )

    confirms: list[str] = []
    playbook: list[str] = ["chase_aware_convex"]
    risk_bits: list[str] = []

    if mny >= 2.5:
        confirms.append(f"far OTM wing ({mny:+.2f}%)")
        playbook.append("far_otm")
        risk_bits.append("far_otm")
    elif mny >= 0.5:
        confirms.append(f"OTM wing ({mny:+.2f}%)")
    else:
        confirms.append(f"near-money ({mny:+.2f}%)")

    if mult3 >= 5.0 or best_mult >= 8.0 or mult5 >= 8.0:
        confirms.append(f"hot convexity (~{max(mult3, best_mult, mult5):.0f}×)")
        playbook.append("asymmetric_payoff")
    elif mult3 >= min_mult_at_3pct:
        confirms.append(f"convex 3%→{mult3:.1f}× / 5%→{mult5:.1f}×")

    if 0.25 <= ask <= 3.0:
        confirms.append(f"cheap-ish chase premium ${ask:.2f}")
    elif ask <= 8.0:
        confirms.append(f"usable chase premium ${ask:.2f}")
        risk_bits.append("richer_ask")
    else:
        confirms.append(f"elevated premium ${ask:.2f} — size small")
        risk_bits.append("elevated_premium")

    if vol >= 500 or oi >= 1000:
        confirms.append(f"liquid (vol {vol} / OI {oi})")
    elif vol >= 50 or oi >= 100:
        confirms.append(f"tradable (vol {vol} / OI {oi})")

    # Chase-aware tape: extension is a FEATURE when impulse continues
    tape = False
    extended = live is not None and live >= 2.0
    if mom5 is not None and mom5 >= min_mom_5m:
        confirms.append(f"5m impulse {mom5:+.2f}%")
        playbook.append("tape_confirm")
        tape = True
    if mom15 is not None and mom15 >= 0.10:
        confirms.append(f"15m trend {mom15:+.2f}%")
        playbook.append("tape_confirm")
        tape = True
    if extended and (mom5 is None or mom5 >= 0.0):
        confirms.append(f"session already +{live:.1f}% — chase-aware (risky)")
        playbook.append("allow_extended")
        risk_bits.append("extended")
        risk_bits.append("chase")
        tape = True  # treat as tape for BUY_RISKY when still non-negative 5m
    elif live is not None and live >= 0.5:
        confirms.append(f"session bid {live:+.2f}%")
        tape = True

    if ens >= 55:
        confirms.append(f"0DTE score {ens:.0f} (soft — not quality-gated)")
    if phase in {"open_drive", "regular", "premarket"}:
        confirms.append(f"session {phase}")

    n_conf = len(confirms)
    strike_txt = f"{float(strike):g}c" if strike is not None else "call"
    risk_tag = risk_bits[0] if risk_bits else "chase"
    setup_ok = n_conf >= 3 and ask <= max_ask and (
        mult3 >= min_mult_at_3pct or best_mult >= 5.0 or mult5 >= 6.0
    )

    if setup_ok and tape:
        strength = min(
            100.0,
            30
            + 7 * n_conf
            + (15 if extended else 0)
            + (10 if mny >= 2.0 else 0)
            + min(25.0, best_mult * 2),
        )
        return ChaseAction(
            action="BUY_RISKY",
            strength=round(strength, 1),
            headline=f"BUY — BIT RISKY {symbol}",
            detail=(
                f"{ticket.get('expiry')} {strike_txt} @ ${ask:.2f} · spot ${spot:.2f} · "
                f"OTM {mny:+.1f}% · ~{mult3:.0f}× if +3% / ~{mult5:.0f}× if +5% · "
                + " · ".join(confirms[:5])
                + " · NOT gated BUY NOW (no hist-win) — size small / discretionary"
            ),
            risk_tag=risk_tag,
            playbook=sorted(set(playbook)),
            confirms=n_conf,
            **base,
        )

    if setup_ok or n_conf >= 2:
        return ChaseAction(
            action="WATCH_CONVEX",
            strength=round(max(lottery_score, 35) * 0.65 + 5 * n_conf, 1),
            headline=f"WATCH CONVEX {symbol}",
            detail=(
                f"{ticket.get('expiry')} {strike_txt} @ ${ask:.2f} · "
                f"{n_conf} confirms — waiting for impulse/extension "
                f"(phase {phase}). "
                + " · ".join(confirms[:4])
            ),
            risk_tag="far_otm" if mny >= 2.5 else "chase",
            playbook=sorted(set(playbook + ["wait_impulse"])),
            confirms=n_conf,
            vetoes=["waiting_impulse"] if not tape else [],
            **base,
        )

    return ChaseAction(
        action="CHASE_COOL",
        strength=round(max(20.0, lottery_score * 0.4), 1),
        headline=f"CHASE COOL {symbol}",
        detail=f"Only {n_conf} confirms — " + (" · ".join(confirms[:3]) or "thin setup"),
        risk_tag="cool",
        playbook=sorted(set(playbook)),
        confirms=n_conf,
        vetoes=["thin_confirms"],
        **base,
    )


def build_chase_board(
    tickets: list[dict[str, Any]],
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    scores: list[dict[str, Any]] | None = None,
    min_ask: float = 0.20,
    max_ask: float = 12.0,
    max_otm_pct: float = 8.0,
    min_mult_at_3pct: float = 3.5,
    min_mom_5m: float = 0.08,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Partition chase tickets into BUY_RISKY / WATCH_CONVEX / COOL."""
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol") or "").upper(): float(s.get("ensemble_score") or 0)
        for s in (scores or [])
    }
    buy_risky: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    cool: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    for t in tickets:
        sym = str(t.get("symbol") or "").upper()
        sig = decide_chase_entry(
            t,
            quote=quotes.get(sym),
            ensemble_score=score_map.get(sym),
            min_ask=min_ask,
            max_ask=max_ask,
            max_otm_pct=max_otm_pct,
            min_mult_at_3pct=min_mult_at_3pct,
            min_mom_5m=min_mom_5m,
            now=now,
        )
        row = sig.to_dict()
        all_rows.append(row)
        if sig.action == "BUY_RISKY":
            buy_risky.append(row)
        elif sig.action == "WATCH_CONVEX":
            watch.append(row)
        else:
            cool.append(row)

    buy_risky.sort(key=lambda r: float(r.get("strength") or 0), reverse=True)
    watch.sort(key=lambda r: float(r.get("strength") or 0), reverse=True)

    return {
        "buy_risky": buy_risky,
        "watch": watch,
        "cool": cool[:8],
        "tickets": all_rows,
        "counts": {
            "buy_risky": len(buy_risky),
            "watch": len(watch),
            "cool": len(cool),
            "all": len(all_rows),
        },
        "note": (
            "Chase / high-convexity lane: far-OTM or already-ripping 0DTE/1DTE calls. "
            "BUY — BIT RISKY ≠ gated Options BUY NOW (no hist-win / anti-chase). "
            "Size small — options can go to zero. Not journaled by default."
        ),
        "score_note": (
            "0DTE ensemble score = weighted average of gap/breakout/volume/RS/VIX/squeeze/"
            "MACD/RSI/EMA algos (0–100). Quality BUY NOW needs score≥72 + ≥3 confirms; "
            "this lane only needs soft score ~55+ and convexity + impulse."
        ),
    }
