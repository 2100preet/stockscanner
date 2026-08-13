"""ML6 BUY NOW / SELL NOW automation (reaction-gated).

Promotes ML6 names to actionable BUY_NOW only after reaction acceptance
(tape + VWAP/AH heuristics). Opens paper journal fills via weekly/swing calls.
SELL_NOW exits open ML6 desk trades on dump / stop / take-profit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from odte_scanner.ml6.watchlist import ML6_WATCHLIST, STATUS_BUY_IF, STATUS_WAIT, STATUS_WATCH


@dataclass
class ML6Action:
    action: str  # BUY_NOW | SELL_NOW | WAIT | HOLD | WATCH
    symbol: str
    strength: float
    headline: str
    detail: str
    score: float | None = None
    status: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    contract: str | None = None
    dte: int | None = None
    dte_bucket: str | None = "weekly"
    live_last: float | None = None
    live_change_pct: float | None = None
    accepted: bool = False
    desk: str = "ml6"
    right: str = "C"
    reasons: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons or [])
        return d


def _live_pct(quote: dict[str, Any] | None) -> float | None:
    if not quote:
        return None
    for k in ("session_change_pct", "change_pct"):
        if quote.get(k) is not None:
            return float(quote[k])
    return None


def reaction_accepted(row: dict[str, Any], quote: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Harden acceptance for automation (still never blind on pre-print)."""
    reasons: list[str] = []
    days = row.get("days_to_earnings")
    if days is not None and int(days) > 0:
        return False, [f"pre-print ({days}d) — no BUY NOW"]

    if row.get("accepted"):
        return True, ["score gate already accepted"]

    if not quote:
        return False, ["no live quote for acceptance"]

    live = _live_pct(quote)
    last = quote.get("last")
    vwap = quote.get("vwap") or quote.get("VWAP")
    ah_high = quote.get("ah_high") or quote.get("post_high")
    mom5 = quote.get("mom_5m_pct")
    mom15 = quote.get("mom_15m_pct")
    dist_high = quote.get("dist_from_day_high_pct")

    # Strong green session post-print / earnings day
    if live is not None and float(live) >= 2.0:
        reasons.append(f"session {float(live):+.1f}%")
        if mom5 is not None and float(mom5) > 0:
            reasons.append(f"5m {float(mom5):+.2f}%")
        if dist_high is not None and float(dist_high) > -1.5:
            reasons.append("holding near day high")
        if vwap is not None and last is not None and float(last) >= float(vwap):
            reasons.append("hold ≥ VWAP")
            return True, reasons
        if ah_high is not None and last is not None and float(last) >= float(ah_high) * 0.995:
            reasons.append("hold AH high")
            return True, reasons
        # Soft accept for peers with strong rip + rising tape even without VWAP field
        if mom5 is not None and float(mom5) >= 0.15 and (mom15 is None or float(mom15) >= 0):
            reasons.append("soft tape accept (green + rising)")
            return True, reasons
        return False, reasons + ["green but missing VWAP/AH/tape confirm"]

    if live is not None and float(live) >= 1.2 and mom5 is not None and float(mom5) >= 0.25:
        reasons.append(f"session {float(live):+.1f}% + 5m impulse")
        if dist_high is None or float(dist_high) > -2.0:
            return True, reasons

    return False, ["no confirmed reaction yet"]


def pick_ml6_call(
    symbol: str,
    *,
    spot: float | None,
    score: float,
    max_dte: int = 21,
    max_ask: float = 15.0,
) -> dict[str, Any] | None:
    """Best listed weekly/swing call for ML6 paper automation."""
    if spot is None or spot <= 0:
        return None
    try:
        from odte_scanner.options.selector import select_calls

        cands = select_calls(
            symbol,
            float(spot),
            float(score),
            ["ml6"],
            max_dte=max_dte,
            odte_max_dte=1,
            otm_pct_max=5.0,
            itm_pct_max=1.0,
            max_ask=max_ask,
            min_open_interest=20,
            min_volume=1,
            per_bucket=1,
        )
    except Exception:  # noqa: BLE001
        return None
    if not cands:
        return None
    # Prefer weekly over accidental 0DTE for earnings swings
    weekly = [c for c in cands if c.dte_bucket == "weekly" or c.dte > 1]
    pick = weekly[0] if weekly else cands[0]
    if pick.synthetic or pick.ask <= 0:
        return None
    return pick.to_dict()


def decide_ml6_entry(
    row: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    open_symbols: set[str] | None = None,
    min_score: float = 70.0,
    attach_call: bool = True,
) -> ML6Action:
    symbol = str(row.get("symbol") or "")
    score = float(row.get("ensemble_score") or row.get("score") or 0)
    status = str(row.get("status") or STATUS_WATCH)
    open_symbols = open_symbols or set()
    live = _live_pct(quote)
    last = None
    if quote and quote.get("last") is not None:
        last = float(quote["last"])
    elif row.get("last_price") is not None:
        last = float(row["last_price"])

    base_kw = dict(
        symbol=symbol,
        score=score,
        status=status,
        live_last=last,
        live_change_pct=live,
        desk="ml6",
    )

    if symbol in open_symbols:
        return ML6Action(
            action="HOLD",
            strength=min(score, 60),
            headline=f"HOLD ML6 {symbol}",
            detail="Already open on ML6 desk — manage with SELL NOW rules.",
            reasons=["already_open"],
            **base_kw,
        )

    if not row.get("liquidity_ok", True):
        return ML6Action(
            action="WAIT",
            strength=score * 0.5,
            headline=f"WAIT ML6 {symbol}",
            detail="Liquidity gate failed — no BUY NOW.",
            reasons=["illiquid"],
            **base_kw,
        )

    days = row.get("days_to_earnings")
    if days is not None and int(days) > 0:
        return ML6Action(
            action="WATCH",
            strength=score * 0.55,
            headline=f"WATCH ML6 {symbol}",
            detail=f"{days}d to earnings — reaction gate; no auto BUY on the print alone.",
            reasons=["pre_print"],
            **base_kw,
        )

    ok, why = reaction_accepted(row, quote)
    if not ok:
        act = "WAIT" if status in {STATUS_WAIT, STATUS_BUY_IF} else "WATCH"
        return ML6Action(
            action=act,
            strength=score * 0.6,
            headline=f"{act} ML6 {symbol}",
            detail=" · ".join(why) or row.get("gate") or "Waiting for reaction confirmation.",
            accepted=False,
            reasons=why,
            **base_kw,
        )

    if score < min_score:
        return ML6Action(
            action="WAIT",
            strength=score * 0.7,
            headline=f"WAIT ML6 {symbol}",
            detail=f"Accepted tape but score {score:.0f} < {min_score:.0f} BUY bar.",
            accepted=True,
            reasons=why + ["score_below_buy"],
            **base_kw,
        )

    call = None
    if attach_call:
        call = pick_ml6_call(symbol, spot=last, score=score)

    if not call:
        return ML6Action(
            action="WAIT",
            strength=score * 0.75,
            headline=f"WAIT ML6 {symbol}",
            detail="Reaction OK but no listed weekly call to automate — review manually.",
            accepted=True,
            reasons=why + ["no_listed_call"],
            **base_kw,
        )

    strength = min(
        100.0,
        0.55 * score
        + 15.0
        + (8 if live and live >= 3 else 0)
        + (5 if status == STATUS_BUY_IF else 0),
    )
    return ML6Action(
        action="BUY_NOW",
        strength=round(strength, 1),
        headline=f"BUY NOW ML6 {symbol}",
        detail=(
            f"{call.get('expiry')} {float(call['strike']):g}c @ ${float(call['ask']):.2f} · "
            f"score {score:.0f} · " + " · ".join(why[:4])
        ),
        accepted=True,
        strike=float(call["strike"]),
        expiry=str(call.get("expiry")),
        ask=float(call["ask"]),
        bid=float(call.get("bid") or 0) or None,
        contract=str(call.get("contract")),
        dte=int(call.get("dte") or 0),
        dte_bucket=str(call.get("dte_bucket") or "weekly"),
        reasons=why,
        **base_kw,
    )


def decide_ml6_exit(
    trade: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
    take_profit_pct: float = 80.0,
    stop_loss_pct: float = 40.0,
) -> ML6Action | None:
    if trade.get("status") != "open":
        return None
    symbol = str(trade.get("symbol") or "")
    # Only manage ML6 desk / ML6 sleeve symbols
    desk = str(trade.get("desk") or trade.get("dte_bucket") or "")
    reason = str(trade.get("entry_reason") or "")
    if symbol not in ML6_WATCHLIST and "ML6" not in reason.upper() and desk != "ml6":
        return None

    entry = float(trade.get("entry_ask") or trade.get("ask") or 0)
    bid = float(trade.get("mark") or trade.get("bid") or 0) or None
    if quote and quote.get("last") and trade.get("strike") is None:
        # share-style — rare
        bid = float(quote["last"])
    live = _live_pct(quote)
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    unreal = ((bid - entry) / entry * 100.0) if bid and entry else None

    reasons: list[str] = []
    sell = False
    if unreal is not None and unreal >= take_profit_pct:
        sell, reasons = True, [f"take profit {unreal:.0f}%"]
    elif unreal is not None and unreal <= -stop_loss_pct:
        sell, reasons = True, [f"stop {unreal:.0f}%"]
    elif mom5 is not None and mom5 <= -1.0:
        sell, reasons = True, [f"5m dump {mom5:+.2f}%"]
    elif live is not None and live <= -3.0:
        sell, reasons = True, [f"session dump {live:+.1f}%"]
    elif row and row.get("blocked_auto_buy") and live is not None and live < 0:
        sell, reasons = True, ["reaction failed — exit ML6 risk"]

    if not sell:
        return ML6Action(
            action="HOLD",
            symbol=symbol,
            strength=55.0,
            headline=f"HOLD ML6 {symbol}",
            detail=f"Open ML6 · unreal {unreal:.0f}%" if unreal is not None else "Open ML6 — manage",
            contract=trade.get("contract"),
            strike=trade.get("strike"),
            expiry=trade.get("expiry"),
            ask=bid,
            bid=bid,
            score=row.get("ensemble_score") if row else trade.get("entry_score"),
            live_change_pct=live,
            desk="ml6",
            reasons=["hold"],
        )

    return ML6Action(
        action="SELL_NOW",
        symbol=symbol,
        strength=88.0,
        headline=f"SELL NOW ML6 {symbol}",
        detail=" · ".join(reasons),
        contract=trade.get("contract"),
        strike=trade.get("strike"),
        expiry=trade.get("expiry"),
        ask=bid,
        bid=bid,
        score=row.get("ensemble_score") if row else trade.get("entry_score"),
        live_change_pct=live,
        desk="ml6",
        reasons=reasons,
    )


def build_ml6_action_board(
    watchlist: list[dict[str, Any]],
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    min_score: float = 70.0,
    attach_calls: bool = True,
) -> dict[str, Any]:
    quotes = quotes or {}
    open_trades = open_trades or []
    open_syms = {str(t.get("symbol")) for t in open_trades if t.get("status") == "open"}
    by_sym = {str(r.get("symbol")): r for r in watchlist}

    buys: list[ML6Action] = []
    waits: list[ML6Action] = []
    watches: list[ML6Action] = []
    holds: list[ML6Action] = []
    sells: list[ML6Action] = []

    for t in open_trades:
        sig = decide_ml6_exit(t, quote=quotes.get(str(t.get("symbol"))), row=by_sym.get(str(t.get("symbol"))))
        if not sig:
            continue
        if sig.action == "SELL_NOW":
            sells.append(sig)
        else:
            holds.append(sig)

    for row in watchlist:
        sig = decide_ml6_entry(
            row,
            quote=quotes.get(str(row.get("symbol"))),
            open_symbols=open_syms,
            min_score=min_score,
            attach_call=attach_calls,
        )
        if sig.action == "BUY_NOW":
            buys.append(sig)
        elif sig.action == "HOLD":
            holds.append(sig)
        elif sig.action == "WATCH":
            watches.append(sig)
        else:
            waits.append(sig)

    buys.sort(key=lambda s: s.strength, reverse=True)
    sells.sort(key=lambda s: s.strength, reverse=True)

    primary = None
    if sells:
        primary = sells[0]
    elif buys:
        primary = buys[0]
    elif waits:
        primary = waits[0]
    elif watches:
        primary = watches[0]

    return {
        "desk": "ml6",
        "primary": primary.to_dict() if primary else None,
        "buy_now": [s.to_dict() for s in buys],
        "sell_now": [s.to_dict() for s in sells],
        "wait": [s.to_dict() for s in waits],
        "watch": [s.to_dict() for s in watches],
        "hold": [s.to_dict() for s in holds],
        "all": [s.to_dict() for s in (sells + buys + holds + waits + watches)],
        "counts": {
            "buy_now": len(buys),
            "sell_now": len(sells),
            "wait": len(waits),
            "watch": len(watches),
            "hold": len(holds),
        },
        "note": (
            "ML6 BUY NOW only after reaction confirmation (never blind on the print). "
            "Paper journal auto-enters/exits when enabled — same pattern as lottery desk."
        ),
    }
