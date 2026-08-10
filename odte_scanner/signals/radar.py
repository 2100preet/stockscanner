"""Discord-style lottery radar — separate from gated BUY NOW.

Catches cheap near-money 0DTE/1DTE wings on liquid indices (SPY/QQQ/IWM…)
like discretionary Discord alerts ($0.20–$2.50 premium, small OTM).

Does NOT feed the paper journal BUY NOW auto-enter path and does NOT
bypass the hist-win / quality gates on the main Options desk.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.signals.lottery import _live_pct, _session_phase

ET = ZoneInfo("America/New_York")

DEFAULT_FOCUS = ("SPY", "QQQ", "IWM", "DIA", "SPX")


@dataclass
class RadarAction:
    action: str  # RADAR_HOT | RADAR_WATCH | RADAR_COOL
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "radar"
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
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    mom_5m_pct: float | None = None
    mom_15m_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_radar_entry(
    ticket: dict[str, Any],
    *,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    min_ask: float = 0.15,
    max_ask: float = 2.50,
    max_otm_pct: float = 1.50,
    min_mult_1pct: float = 1.6,
    now: datetime | None = None,
) -> RadarAction:
    """Classify a cheap wing as HOT / WATCH / COOL for the radar lane."""
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
    best_mult = float(ticket.get("best_mult") or max(mult1, mult2, mult3, 0))
    vol = int(ticket.get("volume") or 0)
    oi = int(ticket.get("open_interest") or 0)

    live = _live_pct(quote)
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    dist_high = (
        float(quote["dist_from_day_high_pct"])
        if quote and quote.get("dist_from_day_high_pct") is not None
        else None
    )
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
        ensemble_score=ens or None,
        live_change_pct=live,
        mom_5m_pct=mom5,
        mom_15m_pct=mom15,
        volume=vol,
        open_interest=oi,
    )

    vetoes: list[str] = []
    if dte > 1:
        vetoes.append("DTE>1 — radar is 0DTE/1DTE wings only")
    if ask <= 0:
        vetoes.append("no ask")
    if ask < min_ask:
        vetoes.append(f"ask ${ask:.2f} below radar floor ${min_ask:.2f}")
    if ask > max_ask:
        vetoes.append(f"ask ${ask:.2f} above cheap-wing ceiling ${max_ask:.2f}")
    if mny > max_otm_pct:
        vetoes.append(f"too far OTM ({mny:+.2f}% > {max_otm_pct:.2f}%)")
    if mny < -0.8:
        vetoes.append(f"too deep ITM ({mny:+.2f}%) — not a lottery wing")
    if best_mult < min_mult_1pct and mult1 < min_mult_1pct and mult2 < 2.0:
        vetoes.append(f"weak wing convexity (1%→{mult1:.1f}×)")
    if phase in {"weekend", "final_30"}:
        vetoes.append(f"session phase {phase} — no new radar alerts")
    if mom5 is not None and mom5 <= -0.15:
        vetoes.append(f"5m dump {mom5:+.2f}% — knife risk")
    if live is not None and live <= -1.0:
        vetoes.append(f"session soft {live:+.2f}%")

    if vetoes:
        return RadarAction(
            action="RADAR_COOL",
            strength=max(0.0, lottery_score * 0.35),
            headline=f"RADAR COOL {symbol}",
            detail="; ".join(vetoes[:3]),
            playbook=["radar_filter"],
            confirms=0,
            vetoes=vetoes,
            **base,
        )

    confirms: list[str] = []
    playbook: list[str] = ["discord_style_radar"]

    # Cheap wing premium (Mike-style $0.28 band)
    if 0.20 <= ask <= 1.25:
        confirms.append(f"cheap wing ${ask:.2f}")
        playbook.append("cheap_premium")
    elif ask <= max_ask:
        confirms.append(f"usable wing ${ask:.2f}")

    # Near-money strike (Mike 776 vs ~773 spot ≈ +0.4% OTM)
    if -0.3 <= mny <= 0.85:
        confirms.append(f"near-money ({mny:+.2f}% vs spot)")
        playbook.append("near_money")
    elif mny <= max_otm_pct:
        confirms.append(f"small OTM ({mny:+.2f}%)")

    if mult1 >= 3.0 or best_mult >= 5.0:
        confirms.append(f"hot convexity (~{max(mult1, best_mult):.0f}×)")
        playbook.append("asymmetric_payoff")
    elif mult1 >= min_mult_1pct or mult2 >= 2.2:
        confirms.append(f"wing convexity 1%→{mult1:.1f}× / 2%→{mult2:.1f}×")

    if vol >= 100 or oi >= 500 or symbol.upper() in DEFAULT_FOCUS:
        confirms.append(f"liquid focus ({symbol} vol {vol} / OI {oi})")
        playbook.append("index_liquidity")
    elif vol >= 20 or oi >= 50:
        confirms.append(f"tradable (vol {vol} / OI {oi})")

    # Soft tape — looser than BUY NOW (radar can alert earlier)
    tape = False
    if mom5 is not None and mom5 >= 0.05:
        confirms.append(f"5m impulse {mom5:+.2f}%")
        playbook.append("tape_confirm")
        tape = True
    if mom15 is not None and mom15 >= 0.08:
        confirms.append(f"15m trend {mom15:+.2f}%")
        playbook.append("tape_confirm")
        tape = True
    if live is not None and live >= 0.08:
        confirms.append(f"session bid {live:+.2f}%")
        playbook.append("session_bid")
        tape = True

    # Reclaim / approach strike (Mike-style: PA into the call wall / strike)
    reclaim = False
    if spot > 0 and strike is not None:
        dist_to_k = (float(strike) - spot) / spot * 100.0
        if 0 <= dist_to_k <= 0.55 and (mom5 is None or mom5 >= 0.0):
            confirms.append(f"approaching strike (gap {dist_to_k:+.2f}%)")
            playbook.append("strike_reclaim")
            reclaim = True
        if dist_high is not None and dist_high >= -0.25 and dist_to_k <= 0.7:
            confirms.append(f"near day highs ({dist_high:+.2f}%)")
            reclaim = True

    if phase in {"open_drive", "regular", "premarket"}:
        confirms.append(f"session {phase}")
        playbook.append("session_timing")

    if ens >= 55:
        confirms.append(f"underlying score {ens:.0f} (soft)")

    n_conf = len(confirms)
    strike_txt = f"{float(strike):g}c" if strike is not None else "call"
    setup_ok = n_conf >= 3 and ask <= max_ask

    if setup_ok and (tape or reclaim):
        strength = min(
            100.0,
            35
            + 8 * n_conf
            + (12 if tape else 0)
            + (10 if reclaim else 0)
            + (8 if 0.20 <= ask <= 0.80 else 0)
            + min(20.0, best_mult),
        )
        return RadarAction(
            action="RADAR_HOT",
            strength=round(strength, 1),
            headline=f"RADAR HOT {symbol}",
            detail=(
                f"{ticket.get('expiry')} {strike_txt} @ ${ask:.2f} · "
                f"spot ${spot:.2f} · ~{mult1:.0f}× if +1% / ~{mult2:.0f}× if +2% · "
                + " · ".join(confirms[:5])
                + " · discretionary lane (not BUY NOW / no hist gate)"
            ),
            playbook=sorted(set(playbook)),
            confirms=n_conf,
            **base,
        )

    if setup_ok or n_conf >= 2:
        return RadarAction(
            action="RADAR_WATCH",
            strength=round(max(lottery_score, 40) * 0.7 + 4 * n_conf, 1),
            headline=f"RADAR WATCH {symbol}",
            detail=(
                f"{ticket.get('expiry')} {strike_txt} @ ${ask:.2f} · "
                f"{n_conf} confirms — waiting for tape/reclaim "
                f"(phase {phase}). "
                + " · ".join(confirms[:4])
            ),
            playbook=sorted(set(playbook + ["wait_tape"])),
            confirms=n_conf,
            vetoes=["waiting_tape_or_reclaim"] if not (tape or reclaim) else [],
            **base,
        )

    return RadarAction(
        action="RADAR_COOL",
        strength=round(lottery_score * 0.4, 1),
        headline=f"RADAR COOL {symbol}",
        detail=f"Only {n_conf} confirms — not a Discord-style wing setup yet.",
        playbook=["radar_filter"],
        confirms=n_conf,
        vetoes=["thin_setup"],
        **base,
    )


def build_radar_board(
    tickets: list[dict[str, Any]],
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    scores: list[dict[str, Any]] | None = None,
    min_ask: float = 0.15,
    max_ask: float = 2.50,
    max_otm_pct: float = 1.50,
    min_mult_1pct: float = 1.6,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rank radar alerts. Never emits BUY_NOW — HOT/WATCH/COOL only."""
    quotes = quotes or {}
    score_map = {
        str(s.get("symbol")): float(s.get("ensemble_score") or s.get("score") or 0)
        for s in (scores or [])
        if s.get("symbol")
    }
    # Prefer 0dte horizon scores when duplicates exist
    for s in scores or []:
        sym = str(s.get("symbol") or "")
        if sym and str(s.get("horizon") or "") == "0dte":
            score_map[sym] = float(s.get("ensemble_score") or s.get("score") or 0)

    hot: list[RadarAction] = []
    watch: list[RadarAction] = []
    cool: list[RadarAction] = []
    for ticket in tickets or []:
        sig = decide_radar_entry(
            ticket,
            quote=quotes.get(str(ticket.get("symbol"))),
            ensemble_score=score_map.get(str(ticket.get("symbol"))),
            min_ask=min_ask,
            max_ask=max_ask,
            max_otm_pct=max_otm_pct,
            min_mult_1pct=min_mult_1pct,
            now=now,
        )
        if sig.action == "RADAR_HOT":
            hot.append(sig)
        elif sig.action == "RADAR_WATCH":
            watch.append(sig)
        else:
            cool.append(sig)

    hot.sort(key=lambda s: (s.strength, s.best_mult or 0), reverse=True)
    watch.sort(key=lambda s: (s.confirms, s.strength), reverse=True)

    primary = hot[0] if hot else (watch[0] if watch else None)
    return {
        "primary": primary.to_dict() if primary else None,
        "hot": [s.to_dict() for s in hot],
        "watch": [s.to_dict() for s in watch[:12]],
        "cool": [s.to_dict() for s in cool[:12]],
        "tickets": tickets[:24],
        "counts": {
            "hot": len(hot),
            "watch": len(watch),
            "cool": len(cool),
            "tickets": len(tickets or []),
        },
        "lane": "radar",
        "note": (
            "Discord-style lottery radar for cheap SPY/QQQ/IWM 0DTE wings. "
            "Separate from BUY NOW — no hist-win gate, no auto journal fills. "
            "Discretionary alerts only."
        ),
    }
