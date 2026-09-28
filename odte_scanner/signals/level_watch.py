"""User TA level-watch lane — support / breakout / targets on sticky names.

Surfaces BUY_LEVEL when spot clears the breakout with support intact,
WATCH_LEVEL while building under the trigger, LEVEL_COOL if support fails.
Feeds BUY/SELL NOW as Levels desk (not hist-gated Options BUY NOW).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Sticky setups from desk notes (support / breakout / upside ladder).
LEVEL_SETUPS: dict[str, dict[str, Any]] = {
    "ALAB": {
        "thesis": "Testing top of multi-month range after breakaway gap",
        "support": 341.0,
        "breakout": 371.0,
        "targets": [401.0],
    },
    "AMAT": {
        "thesis": "ABC retrace done; working out of the base",
        "support": None,
        "breakout": 489.0,
        "targets": [536.0, 575.0, 614.0],
    },
    "AMD": {
        "thesis": "Wave 3 after breakaway + acceleration gaps",
        "support": 620.0,
        "breakout": 646.0,
        "targets": [683.0, 705.0],
    },
    "AXTI": {
        "thesis": "Broke downtrend; holding swing-high AVWAP",
        "support": 77.0,
        "breakout": 80.0,
        "targets": [90.0, 102.0],
    },
    "BE": {
        "thesis": "Range breakout; rising trend structure",
        "support": 277.0,
        "breakout": 300.0,
        "targets": [309.0, 350.0],
    },
    "BMNR": {
        "thesis": "Pushed through multi-week $26.55 range breakout",
        "support": 26.55,
        "breakout": 26.55,
        "targets": [28.67, 35.0],
    },
    "CAT": {
        "thesis": "Tightening in range box after Feb R/S flip",
        "support": 784.0,
        "breakout": 833.0,
        "targets": [843.0, 887.0],
    },
    "DELL": {
        "thesis": "10/21 cloud backtest bounce; rising channel",
        "support": 544.0,
        "breakout": 575.0,
        "targets": [600.0, 626.0],
    },
    "FPS": {
        "thesis": "Double inside day under range top",
        "support": 36.58,
        "breakout": 40.30,
        "targets": [43.0, 48.0],
    },
}


@dataclass
class LevelAction:
    action: str  # BUY_LEVEL | WATCH_LEVEL | LEVEL_COOL
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "levels"
    risk_tag: str = "breakout"  # breakout | building | failed
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    spot: float | None = None
    support: float | None = None
    breakout: float | None = None
    targets: list[float] = field(default_factory=list)
    thesis: str = ""
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    right: str = "C"
    dte_bucket: str = "swing"
    hold_style: str = "level-watch"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def level_symbols() -> list[str]:
    return sorted(LEVEL_SETUPS.keys())


def is_level_symbol(symbol: str) -> bool:
    return str(symbol or "").upper() in LEVEL_SETUPS


def _spot_from(quote: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> float | None:
    q = quote or {}
    c = candidate or {}
    for src in (q, c):
        for k in ("last", "live_last", "live_spot", "spot", "price", "close"):
            v = src.get(k)
            if v is None or v == "":
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f and f > 0:
                return f
    return None


def _live_pct(quote: dict[str, Any] | None) -> float | None:
    q = quote or {}
    for k in ("session_change_pct", "change_pct", "live_change_pct", "day_change_pct"):
        if q.get(k) is not None:
            try:
                return float(q[k])
            except (TypeError, ValueError):
                return None
    return None


def decide_level_action(
    symbol: str,
    *,
    spot: float | None,
    quote: dict[str, Any] | None = None,
    ensemble_score: float | None = None,
    near_breakout_pct: float = 1.5,
) -> LevelAction | None:
    """Classify one sticky TA setup vs live spot."""
    setup = LEVEL_SETUPS.get(str(symbol or "").upper())
    if not setup:
        return None
    sym = str(symbol).upper()
    support = float(setup["support"]) if setup.get("support") is not None else None
    breakout = float(setup["breakout"]) if setup.get("breakout") is not None else None
    targets = [float(t) for t in (setup.get("targets") or [])]
    thesis = str(setup.get("thesis") or "")
    live = _live_pct(quote)
    score = float(ensemble_score or 0)
    playbook = [
        f"support ${support:g}" if support is not None else "support —",
        f"breakout ${breakout:g}" if breakout is not None else "breakout —",
        *(f"tgt ${t:g}" for t in targets[:3]),
    ]
    base = dict(
        symbol=sym,
        support=support,
        breakout=breakout,
        targets=targets,
        thesis=thesis,
        spot=spot,
        ensemble_score=score or None,
        live_change_pct=live,
        playbook=playbook,
    )

    if spot is None or spot <= 0 or breakout is None:
        return LevelAction(
            action="WATCH_LEVEL",
            strength=20.0,
            headline=f"WATCH LEVEL {sym}",
            detail=f"{thesis} · need live spot vs ${breakout:g} breakout" if breakout else thesis,
            risk_tag="building",
            confirms=0,
            **base,
        )

    pct_to_bo = (breakout - spot) / breakout * 100.0
    above_bo = spot >= breakout
    near_bo = (not above_bo) and pct_to_bo <= near_breakout_pct
    support_ok = support is None or spot >= support * 0.995
    failed = support is not None and spot < support * 0.98
    first_tgt = targets[0] if targets else None
    through_first = first_tgt is not None and spot >= first_tgt

    confirms = 0
    if support_ok:
        confirms += 1
    if above_bo:
        confirms += 2
    elif near_bo:
        confirms += 1
    if live is not None and live >= 0:
        confirms += 1
    if score >= 65:
        confirms += 1

    strength = min(
        100.0,
        20
        + (15 if support_ok else 0)
        + (35 if above_bo else (18 if near_bo else 0))
        + max(0.0, live or 0) * 3
        + score * 0.2
        + confirms * 4,
    )

    tgt_txt = " → ".join(f"${t:g}" for t in targets[:3]) or "—"

    if failed:
        return LevelAction(
            action="LEVEL_COOL",
            strength=max(5.0, strength * 0.35),
            headline=f"LEVEL COOL {sym}",
            detail=(
                f"{thesis} · spot ${spot:.2f} lost support ${support:g} "
                f"(breakout ${breakout:g} on hold)"
            ),
            risk_tag="failed",
            confirms=confirms,
            **base,
        )

    if above_bo and support_ok:
        detail = (
            f"{thesis} · spot ${spot:.2f} ≥ breakout ${breakout:g}"
            + (f", support ${support:g} holds" if support is not None else "")
            + f" · ladder {tgt_txt}"
        )
        if through_first:
            detail += f" · already through first tgt ${first_tgt:g} — manage / trail"
        return LevelAction(
            action="BUY_LEVEL",
            strength=strength,
            headline=f"BUY LEVEL {sym}",
            detail=detail,
            risk_tag="breakout",
            confirms=confirms,
            **base,
        )

    if near_bo and support_ok:
        return LevelAction(
            action="WATCH_LEVEL",
            strength=min(strength, 72.0),
            headline=f"WATCH LEVEL {sym}",
            detail=(
                f"{thesis} · spot ${spot:.2f} within {pct_to_bo:.1f}% of "
                f"${breakout:g} breakout · ladder {tgt_txt}"
            ),
            risk_tag="building",
            confirms=confirms,
            **base,
        )

    return LevelAction(
        action="WATCH_LEVEL",
        strength=min(strength, 55.0),
        headline=f"WATCH LEVEL {sym}",
        detail=(
            f"{thesis} · spot ${spot:.2f} · need ${breakout:g} break "
            f"({pct_to_bo:.1f}% away) · support "
            + (f"${support:g}" if support is not None else "—")
            + f" · ladder {tgt_txt}"
        ),
        risk_tag="building",
        confirms=confirms,
        **base,
    )


def build_level_board(
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    scores: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    near_breakout_pct: float = 1.5,
) -> dict[str, Any]:
    """Build BUY_LEVEL / WATCH_LEVEL / LEVEL_COOL board for sticky TA names."""
    quotes = quotes or {}
    score_by = {
        str(r.get("symbol") or "").upper(): float(r.get("ensemble_score") or r.get("score") or 0)
        for r in (scores or [])
        if r.get("symbol")
    }
    cand_by: dict[str, dict[str, Any]] = {}
    for c in candidates or []:
        sym = str(c.get("symbol") or "").upper()
        if sym and sym not in cand_by:
            cand_by[sym] = c

    buy: list[dict[str, Any]] = []
    watch: list[dict[str, Any]] = []
    cool: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    for sym in level_symbols():
        q = quotes.get(sym) or quotes.get(sym.replace("-", ".")) or {}
        spot = _spot_from(q, cand_by.get(sym))
        act = decide_level_action(
            sym,
            spot=spot,
            quote=q,
            ensemble_score=score_by.get(sym),
            near_breakout_pct=near_breakout_pct,
        )
        if act is None:
            continue
        row = act.to_dict()
        all_rows.append(row)
        if act.action == "BUY_LEVEL":
            buy.append(row)
        elif act.action == "LEVEL_COOL":
            cool.append(row)
        else:
            watch.append(row)

    buy.sort(key=lambda r: float(r.get("strength") or 0), reverse=True)
    watch.sort(key=lambda r: float(r.get("strength") or 0), reverse=True)
    cool.sort(key=lambda r: float(r.get("strength") or 0), reverse=True)

    return {
        "label": "Level Watch",
        "purpose": (
            "Sticky TA support / breakout / targets (ALAB·AMAT·AMD·AXTI·BE·BMNR·CAT·DELL·FPS). "
            "BUY_LEVEL when spot clears breakout with support intact — discretionary, not hist-gated Options BUY NOW."
        ),
        "generated_at": None,
        "buy_level": buy,
        "buy_now": buy,
        "watch": watch,
        "cool": cool,
        "all": all_rows,
        "level_symbols": level_symbols(),
        "counts": {
            "buy_level": len(buy),
            "buy_now": len(buy),
            "watch": len(watch),
            "cool": len(cool),
        },
    }
