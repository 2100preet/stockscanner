"""Lottery / parabolic 0DTE–1DTE action engine.

Does NOT list tickets blindly. Applies a multi-factor playbook before
emitting BUY NOW / SELL NOW / WAIT / HOLD — combining:

  • Tape / momentum confirmation (ORB-style short-term reclaim)
  • Liquidity (volume / open interest — avoid dead wings)
  • Convexity gate (only if +2%/+3% rip can produce multi-bagger option P&L)
  • Premium discipline (cheap enough to be a lottery, not a debit sink)
  • Anti-chase / anti-knife (no FOMO into already-extended or dumping tape)
  • Session timing (avoid new lottery entries into the close)
  • Aggressive exits (bank parabolic premium; cut melting tickets fast)

Playbook tags are research labels (not affiliation / not financial advice).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.time_cst import (
    append_asked_cst,
    load_signal_store,
    resolve_first_signal_time,
    save_signal_store,
    signal_timestamps,
)

ET = ZoneInfo("America/New_York")


@dataclass
class LotteryAction:
    action: str  # BUY_NOW | SELL_NOW | WAIT | HOLD | SKIP
    symbol: str
    strength: float
    headline: str
    detail: str
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    vetoes: list[str] = field(default_factory=list)
    contract: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    dte: int | None = None
    lottery_score: float | None = None
    best_mult: float | None = None
    mult_at_2pct: float | None = None
    mult_at_3pct: float | None = None
    mult_at_5pct: float | None = None
    pct_gain_best: float | None = None
    ensemble_score: float | None = None
    live_change_pct: float | None = None
    mom_5m_pct: float | None = None
    mom_15m_pct: float | None = None
    option_unrealized_pct: float | None = None
    trade_id: str | None = None
    signaled_at: str | None = None
    signaled_at_cst: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.action in {"BUY_NOW", "SELL_NOW"} and not d.get("signaled_at"):
            d.update(signal_timestamps())
        return d


def _apply_persisted_lottery(
    sig: LotteryAction,
    store: dict[str, Any],
) -> tuple[LotteryAction, dict[str, Any]]:
    if sig.action not in {"BUY_NOW", "SELL_NOW"}:
        return sig, store
    utc, cst, store = resolve_first_signal_time(store, symbol=sig.symbol, action=sig.action)
    sig.signaled_at = utc
    sig.signaled_at_cst = cst
    sig.detail = append_asked_cst(sig.detail, action=sig.action, signaled_at_cst=cst)
    return sig, store


def _et_now() -> datetime:
    return datetime.now(tz=ET)


def _session_phase(now: datetime | None = None) -> str:
    """Opening drive / power hour / late / closed-ish."""
    now = now or _et_now()
    h = now.hour + now.minute / 60.0
    wd = now.weekday()
    if wd >= 5:
        return "weekend"
    if h < 4:
        return "overnight"
    if 4 <= h < 9.5:
        return "premarket"
    if 9.5 <= h < 10.0:
        return "open_drive"  # first 30m — lottery OK if tape confirms
    if 10.0 <= h < 15.0:
        return "regular"
    if 15.0 <= h < 15.5:
        return "late"  # last hour — tighten
    if 15.5 <= h < 16.0:
        return "final_30"  # usually no new lottery
    return "afterhours"


def _live_pct(quote: dict[str, Any] | None) -> float | None:
    if not quote:
        return None
    if quote.get("session_change_pct") is not None:
        return float(quote["session_change_pct"])
    if quote.get("change_pct") is not None:
        return float(quote["change_pct"])
    return None


def decide_lottery_entry(
    ticket: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    ensemble_score: float | None = None,
    open_contracts: set[str] | None = None,
    min_lottery_score: float = 62.0,
    min_mult_at_3pct: float = 3.0,
    min_confirms: int = 4,
    now: datetime | None = None,
) -> LotteryAction:
    """Gate a convex ticket into BUY NOW or WAIT/SKIP."""
    symbol = str(ticket.get("symbol") or "")
    contract = str(ticket.get("contract") or "")
    ask = float(ticket.get("ask") or 0)
    bid = float(ticket.get("bid") or 0)
    dte = int(ticket.get("dte") if ticket.get("dte") is not None else 99)
    strike = ticket.get("strike")
    lottery_score = float(ticket.get("lottery_score") or 0)
    mult2 = float(ticket.get("mult_at_2pct") or 0)
    mult3 = float(ticket.get("mult_at_3pct") or 0)
    mult5 = float(ticket.get("mult_at_5pct") or 0)
    best_mult = float(ticket.get("best_mult") or 0)
    mny = float(ticket.get("moneyness_pct") or 0)
    vol = int(ticket.get("volume") or 0)
    oi = int(ticket.get("open_interest") or 0)
    ens = float(ensemble_score if ensemble_score is not None else ticket.get("score") or 0)

    live = _live_pct(quote)
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    dist_high = (
        float(quote["dist_from_day_high_pct"])
        if quote and quote.get("dist_from_day_high_pct") is not None
        else None
    )
    last = float(quote["last"]) if quote and quote.get("last") is not None else float(ticket.get("spot") or 0)
    phase = _session_phase(now)

    base = dict(
        symbol=symbol,
        contract=contract or None,
        strike=float(strike) if strike is not None else None,
        expiry=ticket.get("expiry"),
        ask=ask or None,
        bid=bid or None,
        dte=dte,
        lottery_score=lottery_score,
        best_mult=best_mult,
        mult_at_2pct=mult2,
        mult_at_3pct=mult3,
        mult_at_5pct=mult5,
        pct_gain_best=ticket.get("pct_gain_best"),
        ensemble_score=ens,
        live_change_pct=live,
        mom_5m_pct=mom5,
        mom_15m_pct=mom15,
    )

    open_contracts = open_contracts or set()
    if contract and contract in open_contracts:
        return LotteryAction(
            action="HOLD",
            strength=min(100.0, lottery_score),
            headline=f"HOLD LOTTERY {symbol}",
            detail="Already in this lottery contract — manage with SELL NOW rules; don't pyramid.",
            playbook=["position_management"],
            **base,
        )

    confirms: list[str] = []
    vetoes: list[str] = []
    playbook: list[str] = []

    # --- Hard vetoes ---
    if dte > 1:
        vetoes.append("DTE>1 — lottery engine is 0DTE/1DTE only")
    if ask <= 0:
        vetoes.append("no ask")
    if ask > 12:
        vetoes.append(f"ask ${ask:.2f} too rich for lottery (want cheap convexity)")
    if ask < 0.25:
        vetoes.append(f"ask ${ask:.2f} too far junk / illiquid wing")
    if mult3 < min_mult_at_3pct and mult5 < 5.0:
        vetoes.append(f"convexity weak (3% rip → {mult3:.1f}× only)")
    if lottery_score < min_lottery_score:
        vetoes.append(f"lottery score {lottery_score:.0f} < {min_lottery_score:.0f}")
    if phase in {"weekend", "final_30"}:
        vetoes.append(f"session phase {phase} — no new lottery tickets")
    if phase == "late" and (mom5 is None or mom5 < 0.12):
        vetoes.append("late session without strong 5m rip — skip new lottery")

    # Knife / chase (classic 0DTE trader discipline)
    if mom5 is not None and mom5 <= -0.12:
        vetoes.append(f"5m dump {mom5:+.2f}% — no catching knives on lottery")
    if mom15 is not None and mom15 <= -0.22:
        vetoes.append(f"15m trend down {mom15:+.2f}%")
    if live is not None and live <= -0.8:
        vetoes.append(f"session soft {live:+.2f}% — lottery needs a bid, not a fade")
    if live is not None and live >= 2.2 and (mom5 is None or mom5 < 0.08):
        vetoes.append(f"already +{live:.1f}% with no fresh 5m impulse — chase risk")
    if dist_high is not None and dist_high <= -0.55 and (mom5 is None or mom5 <= 0.05):
        vetoes.append(f"off highs {dist_high:+.2f}% without reclaim")
    if last and strike is not None and dte <= 0 and float(last) < float(strike) * 0.997:
        if mom5 is None or mom5 < 0.1:
            vetoes.append("0DTE spot below strike without reclaim tape")

    if vetoes:
        return LotteryAction(
            action="SKIP",
            strength=max(0.0, lottery_score * 0.4),
            headline=f"SKIP LOTTERY {symbol}",
            detail="; ".join(vetoes[:3]),
            playbook=["risk_filter"],
            confirms=0,
            vetoes=vetoes,
            **base,
        )

    # --- Soft confirms (need several) ---
    # 1) Convexity (Hagstrom / Taleb-style asymmetry: defined risk, open upside)
    if mult3 >= 5 or mult5 >= 10:
        confirms.append(f"high convexity ({mult3:.1f}× @+3% / {mult5:.1f}× @+5%)")
        playbook.append("asymmetric_payoff")
    elif mult3 >= min_mult_at_3pct:
        confirms.append(f"convexity ok ({mult3:.1f}× @+3%)")
        playbook.append("asymmetric_payoff")

    # 2) Premium band (lottery sizing — small debit)
    if 0.35 <= ask <= 8.0:
        confirms.append(f"premium band ${ask:.2f}")
        playbook.append("defined_risk_debit")
    elif ask <= 12:
        confirms.append(f"premium usable ${ask:.2f}")

    # 3) Moneyness sweet spot (slightly OTM = leverage; not lottery junk)
    if -0.3 <= mny <= 2.5:
        confirms.append(f"strike sweet spot ({mny:+.2f}% vs spot)")
        playbook.append("otm_leverage")
    elif mny <= 4.0:
        confirms.append(f"OTM ok ({mny:+.2f}%)")

    # 4) Liquidity
    if vol >= 200 or oi >= 500:
        confirms.append(f"liquid (vol {vol} / OI {oi})")
        playbook.append("liquidity_filter")
    elif vol >= 20 or oi >= 50:
        confirms.append(f"tradable (vol {vol} / OI {oi})")

    # 5) Tape confirmation (ORB / momentum desk)
    if mom5 is not None and mom5 >= 0.08:
        confirms.append(f"5m impulse {mom5:+.2f}%")
        playbook.append("tape_confirm")
    if mom15 is not None and mom15 >= 0.1:
        confirms.append(f"15m trend {mom15:+.2f}%")
        playbook.append("tape_confirm")
    if live is not None and 0.15 <= live <= 1.8:
        confirms.append(f"session bid {live:+.2f}% (not exhausted)")
        playbook.append("trend_day_bias")

    # 6) Underlying ensemble quality (multi-algo stack)
    if ens >= 70:
        confirms.append(f"underlying quality score {ens:.0f}")
        playbook.append("multi_algo_stack")
    elif ens >= 62:
        confirms.append(f"underlying score {ens:.0f}")

    # 7) Opening drive / power-hour timing
    if phase in {"open_drive", "regular", "premarket"}:
        confirms.append(f"session phase {phase}")
        playbook.append("session_timing")

    # 8) Lottery composite score
    if lottery_score >= 75:
        confirms.append(f"lottery score {lottery_score:.0f} (top tier)")
    elif lottery_score >= min_lottery_score:
        confirms.append(f"lottery score {lottery_score:.0f}")

    n_conf = len(confirms)
    # Require tape for BUY NOW (5m / 15m impulse or constructive session bid)
    has_tape = any(x.startswith("5m") or x.startswith("15m") or x.startswith("session bid") for x in confirms)
    if not has_tape:
        return LotteryAction(
            action="WAIT",
            strength=lottery_score * 0.7,
            headline=f"WAIT LOTTERY {symbol}",
            detail=(
                f"Convex ticket ({n_conf} confirms), but no tape confirm yet "
                f"(phase {phase}) — wait for 5m/15m reclaim before BUY NOW."
            ),
            playbook=sorted(set(playbook + ["tape_confirm"])),
            confirms=n_conf,
            vetoes=["missing_tape_confirm"],
            **base,
        )

    if n_conf >= min_confirms and has_tape and lottery_score >= min_lottery_score:
        strength = min(
            100.0,
            0.45 * lottery_score
            + 0.2 * min(100.0, ens)
            + 0.2 * min(100.0, mult3 * 12)
            + 5.0 * n_conf
            + (8 if mom5 and mom5 > 0.15 else 0),
        )
        strike_txt = f"{float(strike):g}c" if strike is not None else "call"
        return LotteryAction(
            action="BUY_NOW",
            strength=round(strength, 1),
            headline=f"BUY NOW LOTTERY {symbol}",
            detail=(
                f"{ticket.get('expiry')} {strike_txt} @ ${ask:.2f} · "
                f"~{mult3:.0f}× if +3% / ~{mult5:.0f}× if +5% · "
                + " · ".join(confirms[:5])
            ),
            playbook=sorted(set(playbook)),
            confirms=n_conf,
            **base,
        )

    return LotteryAction(
        action="WAIT",
        strength=round(lottery_score * 0.65, 1),
        headline=f"WAIT LOTTERY {symbol}",
        detail=(
            f"Only {n_conf}/{min_confirms} confirms — "
            + (", ".join(confirms[:4]) if confirms else "building setup")
        ),
        playbook=sorted(set(playbook)),
        confirms=n_conf,
        vetoes=[] if n_conf else ["insufficient_confirms"],
        **base,
    )


def decide_lottery_exit(
    trade: dict[str, Any],
    *,
    quote: dict[str, Any] | None,
    ticket: dict[str, Any] | None = None,
    mark: float | None = None,
    take_profit_pct: float = 120.0,
    runner_pct: float = 300.0,
    stop_loss_pct: float = 45.0,
    now: datetime | None = None,
) -> LotteryAction | None:
    """Aggressive lottery exits — bank parabolic, cut melts, time-stop late."""
    if trade.get("status") != "open":
        return None
    symbol = str(trade.get("symbol") or "")
    entry = float(trade.get("entry_ask") or trade.get("ask") or 0)
    t_bid = float(ticket.get("bid") or 0) if ticket else 0.0
    t_ask = float(ticket.get("ask") or 0) if ticket else 0.0
    # Exit mark: prefer live sellable bid / passed mark, never a stale entry echo.
    bid = None
    if mark is not None and mark > 0:
        bid = float(mark)
    elif t_bid > 0:
        bid = t_bid
    elif trade.get("mark") and float(trade["mark"]) > 0:
        bid = float(trade["mark"])
    elif trade.get("bid") and float(trade["bid"]) > 0:
        bid = float(trade["bid"])
    elif t_ask > 0:
        bid = t_ask

    # If mark is still pinned to entry but the live ticket has collapsed, use ticket.
    if entry > 0 and t_ask > 0:
        stale_at_entry = bid is None or abs(float(bid) - entry) < 1e-9
        ask_melted = (t_ask - entry) / entry <= -0.2
        if stale_at_entry and ask_melted:
            bid = t_ask if t_bid <= 0 else min(t_bid, t_ask)
        elif t_bid > 0 and (bid is None or abs(float(bid) - entry) < 1e-9):
            bid = t_bid

    live = _live_pct(quote)
    mom5 = float(quote["mom_5m_pct"]) if quote and quote.get("mom_5m_pct") is not None else None
    mom15 = float(quote["mom_15m_pct"]) if quote and quote.get("mom_15m_pct") is not None else None
    phase = _session_phase(now)
    unreal = ((bid - entry) / entry * 100.0) if bid and entry else None

    base = dict(
        symbol=symbol,
        contract=trade.get("contract"),
        strike=trade.get("strike"),
        expiry=trade.get("expiry"),
        ask=bid,  # exit pricing — never entry ask (that zeroed P&L on fills)
        bid=bid,
        dte=trade.get("dte"),
        trade_id=trade.get("id"),
        live_change_pct=live,
        mom_5m_pct=mom5,
        mom_15m_pct=mom15,
        option_unrealized_pct=unreal,
        ensemble_score=trade.get("entry_score"),
    )

    reasons: list[str] = []
    playbook: list[str] = []
    strength = 50.0

    if unreal is not None and unreal >= runner_pct:
        reasons.append(f"parabolic +{unreal:.0f}% — bank lottery winner")
        playbook.append("scale_out_parabola")
        strength = 98.0
    elif unreal is not None and unreal >= take_profit_pct:
        reasons.append(f"hit +{unreal:.0f}% target — take lottery profit")
        playbook.append("take_profit")
        strength = 92.0

    if unreal is not None and unreal <= -stop_loss_pct:
        reasons.append(f"premium melted {unreal:.0f}% — cut lottery loser")
        playbook.append("hard_stop")
        strength = max(strength, 95.0)

    if mom5 is not None and mom5 <= -0.25:
        reasons.append(f"5m dump {mom5:+.2f}% — exit convex long")
        playbook.append("tape_fail")
        strength = max(strength, 88.0)
    if mom15 is not None and mom15 <= -0.4:
        reasons.append(f"15m breakdown {mom15:+.2f}%")
        playbook.append("tape_fail")
        strength = max(strength, 90.0)
    if live is not None and live <= -1.2:
        reasons.append(f"session rolled over {live:+.2f}%")
        playbook.append("trend_fail")
        strength = max(strength, 85.0)

    # Desk practice: hard flatten lottery / 0DTE by ~15:45 ET (gamma into the close)
    from odte_scanner.signals.hold_rules import past_odte_flatten

    if past_odte_flatten(now) or phase in {"final_30", "late"}:
        reasons.append("time-stop — flatten lottery by 15:45 ET (no gamma into the close)")
        playbook.append("time_stop")
        strength = max(strength, 88.0)

    # Option mark collapsing vs entry even if underlying flat
    if entry > 0 and t_ask > 0 and (t_ask - entry) / entry <= -0.4:
        reasons.append(f"live ask ${t_ask:.2f} vs entry ${entry:.2f} (−40%+)")
        playbook.append("premium_decay")
        strength = max(strength, 86.0)
        # Ensure SELL NOW carries the live print (not entry-priced mark)
        if bid is None or abs(float(bid) - entry) < 1e-9 or float(bid) > t_ask:
            live_exit = t_bid if t_bid > 0 else t_ask
            bid = live_exit
            unreal = ((bid - entry) / entry * 100.0) if entry else unreal
            base["bid"] = bid
            base["ask"] = bid
            base["option_unrealized_pct"] = unreal

    if reasons:
        return LotteryAction(
            action="SELL_NOW",
            strength=round(strength, 1),
            headline=f"SELL NOW LOTTERY {symbol}",
            detail="; ".join(reasons[:3]),
            playbook=sorted(set(playbook)),
            confirms=len(reasons),
            **base,
        )

    return LotteryAction(
        action="HOLD",
        strength=55.0,
        headline=f"HOLD LOTTERY {symbol}",
        detail=(
            f"Lottery still valid"
            + (f" · unreal {unreal:+.0f}%" if unreal is not None else "")
            + " — trail; sell on tape break or +120%/+300%."
        ),
        playbook=["position_management"],
        confirms=0,
        **base,
    )


def _prefer_0dte_scores(scores: list[dict[str, Any]] | None) -> dict[str, float]:
    """Map symbol → ensemble score, preferring the 0DTE horizon for lottery tape."""
    best: dict[str, tuple[float, str]] = {}
    for s in scores or []:
        sym = str(s.get("symbol") or "")
        if not sym:
            continue
        hz = str(s.get("horizon") or "")
        ens = float(s.get("ensemble_score") or s.get("score") or 0)
        prev = best.get(sym)
        if prev is None or hz == "0dte" or (prev[1] != "0dte" and ens > prev[0]):
            best[sym] = (ens, hz)
    return {k: v[0] for k, v in best.items()}


def _is_lottery_open_trade(
    trade: dict[str, Any],
    *,
    by_contract: dict[str, dict[str, Any]],
) -> bool:
    bucket = str(trade.get("dte_bucket") or trade.get("style") or "").lower()
    if bucket in {"0dte", "1dte", "lottery", "explosive"}:
        return True
    dte = trade.get("dte")
    if dte is not None and int(dte) <= 1:
        return True
    return str(trade.get("contract") or "") in by_contract


def build_lottery_board(
    explosive: list[dict[str, Any]],
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    scores: list[dict[str, Any]] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    min_lottery_score: float = 62.0,
    min_confirms: int = 4,
    now: datetime | None = None,
    signal_times_path: str | None = "outputs/lottery_signal_times.json",
) -> dict[str, Any]:
    """Ranked lottery actions — only promote BUY/SELL when playbook clears."""
    quotes = quotes or {}
    score_map = _prefer_0dte_scores(scores)
    open_all = [t for t in (open_trades or []) if t.get("status") == "open"]
    open_contracts = {str(t.get("contract")) for t in open_all if t.get("contract")}
    store = load_signal_store(signal_times_path)

    # Map tickets by contract for exit marks
    by_contract = {str(t.get("contract")): t for t in explosive if t.get("contract")}
    by_symbol_best: dict[str, dict[str, Any]] = {}
    for t in explosive:
        sym = str(t.get("symbol"))
        prev = by_symbol_best.get(sym)
        if prev is None or float(t.get("lottery_score") or 0) > float(prev.get("lottery_score") or 0):
            by_symbol_best[sym] = t

    sells: list[LotteryAction] = []
    holds: list[LotteryAction] = []
    for t in open_all:
        if not _is_lottery_open_trade(t, by_contract=by_contract):
            continue
        ticket = by_contract.get(str(t.get("contract"))) or by_symbol_best.get(str(t.get("symbol")))
        # Prefer journal/ledger mark (true MTM); fall back to live ticket bid
        mark = None
        if t.get("mark"):
            mark = float(t["mark"])
        elif ticket and ticket.get("bid"):
            mark = float(ticket["bid"])
        sig = decide_lottery_exit(
            t,
            quote=quotes.get(str(t.get("symbol"))),
            ticket=ticket,
            mark=mark,
            now=now,
        )
        if not sig:
            continue
        if sig.action == "SELL_NOW":
            sig, store = _apply_persisted_lottery(sig, store)
            sells.append(sig)
        else:
            holds.append(sig)

    buys: list[LotteryAction] = []
    waits: list[LotteryAction] = []
    skips: list[LotteryAction] = []
    for ticket in explosive:
        sig = decide_lottery_entry(
            ticket,
            quote=quotes.get(str(ticket.get("symbol"))),
            ensemble_score=score_map.get(str(ticket.get("symbol"))),
            open_contracts=open_contracts,
            min_lottery_score=min_lottery_score,
            min_confirms=min_confirms,
            now=now,
        )
        if sig.action == "BUY_NOW":
            sig, store = _apply_persisted_lottery(sig, store)
            buys.append(sig)
        elif sig.action == "WAIT":
            waits.append(sig)
        elif sig.action == "HOLD":
            holds.append(sig)
        else:
            skips.append(sig)

    buys.sort(key=lambda s: (s.strength, s.best_mult or 0), reverse=True)
    sells.sort(key=lambda s: s.strength, reverse=True)
    waits.sort(key=lambda s: (s.confirms, s.lottery_score or 0), reverse=True)

    save_signal_store(signal_times_path, store)

    primary = None
    if sells:
        primary = sells[0]
    elif buys:
        primary = buys[0]

    return {
        "primary": primary.to_dict() if primary else None,
        "buy_now": [s.to_dict() for s in buys],
        "sell_now": [s.to_dict() for s in sells],
        "wait": [s.to_dict() for s in waits[:12]],
        "hold": [s.to_dict() for s in holds],
        "skip": [s.to_dict() for s in skips[:12]],
        "counts": {
            "buy_now": len(buys),
            "sell_now": len(sells),
            "wait": len(waits),
            "hold": len(holds),
            "skip": len(skips),
        },
        "signal_times": store,
        "playbook_note": (
            "Lottery BUY NOW requires convexity + liquidity + tape confirm + session timing "
            "+ underlying multi-algo score. SELL NOW banks +120%/+300% or cuts on tape/premium fail. "
            "BUY NOW time shown in US Central (CST/CDT) from the first pulse. "
            "Research only — options can go to zero."
        ),
    }
