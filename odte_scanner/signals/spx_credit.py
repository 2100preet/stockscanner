"""SPX 0DTE iron-condor credit playbook (SuperLuckeee-style).

Playbook from public desk screenshots / 0DTE credit IC practice:
  • Sell 10-wide iron condor on SPX weeklies (0DTE preferred)
  • Short strikes ~45–70 pts (~0.5–1.0%) OTM each side
  • Target net credit ≈ $0.90 for the package
  • Hard stop: buy-to-close if debit-to-close ≥ $1.40
  • Bank when remaining debit ≤ profit target (default 50% of credit)
  • No new entries after 15:00 ET; flatten by 15:45 ET

Paper / signal desk only — not auto-brokered.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
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

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

DEFAULT_SYMBOLS = ("SPX", "XSP")
NO_NEW_ENTRIES_ET = time(15, 0)
FLATTEN_ET = time(15, 45)

# Sticky actions for asked-time stamps
_ENTRY_ACTIONS = frozenset({"SELL_CREDIT", "BUY_NOW", "ENTRY"})
_EXIT_ACTIONS = frozenset({"BUY_TO_CLOSE", "SELL_NOW", "EXIT"})


@dataclass
class CondorLeg:
    side: str  # sell_put | buy_put | sell_call | buy_call
    right: str  # P | C
    strike: float
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    contract: str | None = None
    delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpxCreditAction:
    action: str  # SELL_CREDIT | WAIT | BUY_TO_CLOSE | HOLD | SKIP
    symbol: str
    strength: float
    headline: str
    detail: str
    lane: str = "spx_credit"
    structure: str = "iron_condor"
    expiry: str | None = None
    dte: int | None = None
    spot: float | None = None
    wing_width: float = 10.0
    credit: float | None = None
    debit_to_close: float | None = None
    stop_debit: float = 1.40
    target_credit: float = 0.90
    profit_take_debit: float | None = None
    max_loss: float | None = None
    short_put: float | None = None
    long_put: float | None = None
    short_call: float | None = None
    long_call: float | None = None
    put_credit: float | None = None
    call_credit: float | None = None
    legs: list[dict[str, Any]] = field(default_factory=list)
    playbook: list[str] = field(default_factory=list)
    confirms: int = 0
    vetoes: list[str] = field(default_factory=list)
    right: str = "IC"
    ask: float | None = None  # credit shown as positive premium collected
    bid: float | None = None  # debit to close
    entry_ask: float | None = None  # filled / paper entry credit
    contract: str | None = None  # composite key
    signaled_at: str | None = None
    signaled_at_cst: str | None = None
    exit_plan: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.action in _ENTRY_ACTIONS | _EXIT_ACTIONS and not d.get("signaled_at"):
            d.update(signal_timestamps())
        # Now-board compatibility: treat open credit as BUY-style entry pulse
        if self.action == "SELL_CREDIT":
            d["alert_action"] = "BUY_NOW"
            d["desk_action"] = "SELL_CREDIT"
        elif self.action == "BUY_TO_CLOSE":
            d["alert_action"] = "SELL_NOW"
            d["desk_action"] = "BUY_TO_CLOSE"
        return d


def _apply_persisted(
    sig: SpxCreditAction,
    store: dict[str, Any],
) -> tuple[SpxCreditAction, dict[str, Any]]:
    act = sig.action
    sticky = "BUY_NOW" if act == "SELL_CREDIT" else ("SELL_NOW" if act == "BUY_TO_CLOSE" else act)
    if sticky not in {"BUY_NOW", "SELL_NOW"}:
        return sig, store
    utc, cst, store = resolve_first_signal_time(
        store,
        symbol=sig.symbol,
        action=sticky,
        contract=sig.contract,
    )
    sig.signaled_at = utc
    sig.signaled_at_cst = cst
    sig.detail = append_asked_cst(sig.detail, action=sticky, signaled_at_cst=cst)
    return sig, store


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _round_strike(spot: float, *, width: float, symbol: str) -> float:
    """SPX strikes are typically 5-pt; keep XSP on 1-pt when small."""
    step = 5.0 if str(symbol).upper() == "SPX" or spot >= 500 else 1.0
    # Snap width itself
    w = max(step, round(float(width) / step) * step)
    return w


def _snap(x: float, step: float) -> float:
    return round(round(x / step) * step, 2)


def _today_et(now: datetime | None = None) -> date:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date()


def _phase(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(ET)
    if local.weekday() >= 5:
        return "weekend"
    t = local.time()
    if t < time(10, 0):
        return "open_drive"  # wait for first ~30m settle before new credit ICs
    if t < NO_NEW_ENTRIES_ET:
        return "regular"
    if t < FLATTEN_ET:
        return "late"
    return "final_30"


def estimate_spread_credit(short_bid: float | None, long_ask: float | None) -> float | None:
    """Natural credit for a vertical: short bid − long ask."""
    sb = _f(short_bid)
    la = _f(long_ask)
    if sb is None or la is None:
        return None
    return round(sb - la, 2)


def estimate_spread_debit(short_ask: float | None, long_bid: float | None) -> float | None:
    """Natural debit to close a vertical: short ask − long bid."""
    sa = _f(short_ask)
    lb = _f(long_bid)
    if sa is None or lb is None:
        return None
    return round(max(0.0, sa - lb), 2)


def _leg_quote(chain: list[dict[str, Any]], *, right: str, strike: float) -> dict[str, Any] | None:
    want = str(right).upper()[:1]
    target = float(strike)
    best = None
    best_diff = 1e18
    for o in chain or []:
        if not isinstance(o, dict):
            continue
        r = str(o.get("option_type") or o.get("right") or "").upper()[:1]
        if r not in {"C", "P"}:
            # Tradier uses put/call
            ot = str(o.get("option_type") or "").lower()
            r = "P" if ot.startswith("p") else ("C" if ot.startswith("c") else "")
        if r != want:
            continue
        k = _f(o.get("strike"))
        if k is None:
            continue
        diff = abs(k - target)
        if diff < best_diff:
            best_diff = diff
            best = o
    if best is None or best_diff > 0.51:
        return None
    return best


def _mid(bid: float | None, ask: float | None) -> float | None:
    b, a = _f(bid), _f(ask)
    if b is not None and a is not None and b > 0 and a > 0:
        return round((b + a) / 2.0, 2)
    return a if a is not None else b


def pick_iron_condor(
    chain: list[dict[str, Any]],
    *,
    symbol: str,
    spot: float,
    expiry: str,
    dte: int = 0,
    wing_width: float = 10.0,
    short_otm_pts: float = 50.0,
    target_credit: float = 0.90,
    min_credit: float = 0.70,
    max_credit: float = 1.40,
    credit_tol: float = 0.25,
) -> dict[str, Any] | None:
    """Pick a 10-wide SPX iron condor near target credit from a flat option chain."""
    if spot <= 0 or not chain:
        return None
    sym = str(symbol).upper()
    step = 5.0 if sym == "SPX" or spot >= 500 else 1.0
    width = _round_strike(spot, width=wing_width, symbol=sym)
    # Scale XSP (~1/10 SPX) distances
    otm = float(short_otm_pts)
    if sym == "XSP":
        otm = max(step, otm / 10.0)
        width = max(step, width / 10.0 if wing_width >= 10 else width)
        target_credit = target_credit / 10.0
        min_credit = min_credit / 10.0
        max_credit = max_credit / 10.0
        credit_tol = credit_tol / 10.0

    short_put_k = _snap(spot - otm, step)
    long_put_k = _snap(short_put_k - width, step)
    short_call_k = _snap(spot + otm, step)
    long_call_k = _snap(short_call_k + width, step)

    # Search nearby short strikes for best credit near target
    candidates: list[dict[str, Any]] = []
    for put_shift in (-2 * step, -step, 0.0, step, 2 * step):
        for call_shift in (-2 * step, -step, 0.0, step, 2 * step):
            sp = _snap(short_put_k + put_shift, step)
            lp = _snap(sp - width, step)
            sc = _snap(short_call_k + call_shift, step)
            lc = _snap(sc + width, step)
            if sp >= spot or sc <= spot or lp >= sp or lc <= sc:
                continue
            q_sp = _leg_quote(chain, right="P", strike=sp)
            q_lp = _leg_quote(chain, right="P", strike=lp)
            q_sc = _leg_quote(chain, right="C", strike=sc)
            q_lc = _leg_quote(chain, right="C", strike=lc)
            if not all((q_sp, q_lp, q_sc, q_lc)):
                continue
            put_cred = estimate_spread_credit(q_sp.get("bid"), q_lp.get("ask"))
            call_cred = estimate_spread_credit(q_sc.get("bid"), q_lc.get("ask"))
            if put_cred is None or call_cred is None:
                continue
            credit = round(put_cred + call_cred, 2)
            if credit < min_credit or credit > max_credit:
                continue
            put_debit = estimate_spread_debit(q_sp.get("ask"), q_lp.get("bid"))
            call_debit = estimate_spread_debit(q_sc.get("ask"), q_lc.get("bid"))
            debit = None
            if put_debit is not None and call_debit is not None:
                debit = round(put_debit + call_debit, 2)
            score = -abs(credit - target_credit) * 10.0 + credit
            # Prefer balanced wings (similar otm distance)
            bal = abs(abs(spot - sp) - abs(sc - spot))
            score -= bal * 0.02
            legs = [
                CondorLeg(
                    "sell_put",
                    "P",
                    sp,
                    _f(q_sp.get("bid")),
                    _f(q_sp.get("ask")),
                    _mid(q_sp.get("bid"), q_sp.get("ask")),
                    str(q_sp.get("symbol") or q_sp.get("contract") or ""),
                    _f((q_sp.get("greeks") or {}).get("delta") if isinstance(q_sp.get("greeks"), dict) else q_sp.get("delta")),
                ),
                CondorLeg(
                    "buy_put",
                    "P",
                    lp,
                    _f(q_lp.get("bid")),
                    _f(q_lp.get("ask")),
                    _mid(q_lp.get("bid"), q_lp.get("ask")),
                    str(q_lp.get("symbol") or q_lp.get("contract") or ""),
                ),
                CondorLeg(
                    "sell_call",
                    "C",
                    sc,
                    _f(q_sc.get("bid")),
                    _f(q_sc.get("ask")),
                    _mid(q_sc.get("bid"), q_sc.get("ask")),
                    str(q_sc.get("symbol") or q_sc.get("contract") or ""),
                    _f((q_sc.get("greeks") or {}).get("delta") if isinstance(q_sc.get("greeks"), dict) else q_sc.get("delta")),
                ),
                CondorLeg(
                    "buy_call",
                    "C",
                    lc,
                    _f(q_lc.get("bid")),
                    _f(q_lc.get("ask")),
                    _mid(q_lc.get("bid"), q_lc.get("ask")),
                    str(q_lc.get("symbol") or q_lc.get("contract") or ""),
                ),
            ]
            candidates.append(
                {
                    "score": score,
                    "credit": credit,
                    "debit_to_close": debit,
                    "put_credit": put_cred,
                    "call_credit": call_cred,
                    "short_put": sp,
                    "long_put": lp,
                    "short_call": sc,
                    "long_call": lc,
                    "wing_width": width,
                    "legs": [x.to_dict() for x in legs],
                    "contract": f"{sym}:{expiry}:IC:{sp:g}/{lp:g}-{sc:g}/{lc:g}",
                }
            )

    if not candidates:
        return None
    # Prefer credits within tolerance of target, else closest
    near = [c for c in candidates if abs(c["credit"] - target_credit) <= credit_tol]
    pool = near or candidates
    pool.sort(key=lambda c: (-c["score"], -c["credit"]))
    best = pool[0]
    best.update(
        {
            "symbol": sym,
            "expiry": expiry,
            "dte": dte,
            "spot": round(float(spot), 2),
            "target_credit": target_credit,
            "max_loss": round(width - best["credit"], 2),
        }
    )
    return best


def decide_spx_credit_entry(
    package: dict[str, Any],
    *,
    quote: dict[str, Any] | None = None,
    target_credit: float = 0.90,
    min_credit: float = 0.70,
    stop_debit: float = 1.40,
    now: datetime | None = None,
    open_contracts: set[str] | None = None,
) -> SpxCreditAction:
    """Gate a built IC package into SELL_CREDIT / WAIT / SKIP."""
    symbol = str(package.get("symbol") or "SPX").upper()
    credit = _f(package.get("credit"))
    spot = _f(package.get("spot"))
    width = float(package.get("wing_width") or 10.0)
    phase = _phase(now)
    contract = str(package.get("contract") or "")
    live = None
    q = quote or {}
    for k in ("session_change_pct", "change_pct", "live_change_pct"):
        if q.get(k) is not None:
            live = float(q[k])
            break

    base = dict(
        symbol=symbol,
        expiry=package.get("expiry"),
        dte=package.get("dte"),
        spot=spot,
        wing_width=width,
        credit=credit,
        debit_to_close=_f(package.get("debit_to_close")),
        stop_debit=float(stop_debit),
        target_credit=float(target_credit),
        profit_take_debit=round(float(target_credit) * 0.50, 2) if target_credit else None,
        max_loss=_f(package.get("max_loss")),
        short_put=_f(package.get("short_put")),
        long_put=_f(package.get("long_put")),
        short_call=_f(package.get("short_call")),
        long_call=_f(package.get("long_call")),
        put_credit=_f(package.get("put_credit")),
        call_credit=_f(package.get("call_credit")),
        legs=list(package.get("legs") or []),
        contract=contract or None,
        ask=credit,
        bid=_f(package.get("debit_to_close")),
        entry_ask=credit,
    )
    plan = (
        f"EXIT plan · buy-to-close stop ≥${stop_debit:.2f} · "
        f"bank ≤${(float(target_credit) * 0.50):.2f} debit · flatten by 15:45 ET"
    )
    base["exit_plan"] = plan

    vetoes: list[str] = []
    if phase in {"weekend", "final_30"}:
        vetoes.append(f"session phase {phase} — no new credit ICs")
    if phase == "late":
        vetoes.append("past 15:00 ET — no new 0DTE credit entries")
    if credit is None:
        vetoes.append("no package credit")
    elif credit < min_credit:
        vetoes.append(f"credit ${credit:.2f} < min ${min_credit:.2f}")
    if open_contracts and contract and contract in open_contracts:
        return SpxCreditAction(
            action="HOLD",
            strength=70.0,
            headline=f"HOLD {symbol} IC",
            detail=f"Already short this condor — manage with BUY_TO_CLOSE rules. {plan}",
            playbook=["position_management"],
            confirms=1,
            **base,
        )

    if vetoes:
        return SpxCreditAction(
            action="SKIP",
            strength=20.0,
            headline=f"SKIP {symbol} IC",
            detail="; ".join(vetoes[:3]),
            playbook=["risk_filter"],
            vetoes=vetoes,
            **base,
        )

    confirms: list[str] = []
    if credit is not None and abs(credit - target_credit) <= 0.20:
        confirms.append(f"credit ${credit:.2f} near target ${target_credit:.2f}")
    elif credit is not None:
        confirms.append(f"credit ${credit:.2f}")
    if package.get("put_credit") is not None and package.get("call_credit") is not None:
        confirms.append(
            f"put ${float(package['put_credit']):.2f} + call ${float(package['call_credit']):.2f}"
        )
    if spot and package.get("short_put") and package.get("short_call"):
        confirms.append(
            f"shorts {float(package['short_put']):g}P / {float(package['short_call']):g}C "
            f"(±{abs(spot - float(package['short_put'])):.0f}/"
            f"{abs(float(package['short_call']) - spot):.0f} pts)"
        )
    confirms.append(f"{width:g}-wide wings")
    if phase == "regular":
        confirms.append("session phase regular")
    elif phase == "open_drive":
        confirms.append("open drive — size small / wait for settle")

    # Extreme tape: still allow but wait if ripping hard (IC hates trend days)
    if live is not None and abs(live) >= 1.2:
        return SpxCreditAction(
            action="WAIT",
            strength=55.0,
            headline=f"WAIT {symbol} IC",
            detail=(
                f"Credit package ready (${credit:.2f}) but session {live:+.2f}% — "
                f"trend day risk for iron condor. {plan}"
            ),
            playbook=["tape_filter", "defined_risk_credit"],
            confirms=len(confirms),
            vetoes=["trend_day"],
            **base,
        )

    if phase == "open_drive":
        return SpxCreditAction(
            action="WAIT",
            strength=60.0,
            headline=f"WAIT {symbol} IC",
            detail=(
                f"IC built for ${credit:.2f} credit — wait for first 5–15m settle "
                f"before SELL CREDIT. {plan}"
            ),
            playbook=["session_timing", "defined_risk_credit"],
            confirms=len(confirms),
            **base,
        )

    strength = min(
        100.0,
        55.0
        + 20.0 * max(0.0, 1.0 - abs((credit or 0) - target_credit) / max(target_credit, 0.01))
        + 4.0 * len(confirms),
    )
    return SpxCreditAction(
        action="SELL_CREDIT",
        strength=round(strength, 1),
        headline=f"SELL CREDIT {symbol} IC · ${credit:.2f}",
        detail=(
            f"{symbol} {package.get('expiry')} iron condor · "
            f"sell {package.get('short_put'):g}/{package.get('long_put'):g}P + "
            f"{package.get('short_call'):g}/{package.get('long_call'):g}C · "
            f"credit ${credit:.2f} · stop ${stop_debit:.2f} · "
            + " · ".join(confirms[:4])
            + f" · {plan}"
        ),
        playbook=["defined_risk_credit", "iron_condor", "0dte_theta"],
        confirms=len(confirms),
        **base,
    )


def decide_spx_credit_exit(
    trade: dict[str, Any],
    *,
    package: dict[str, Any] | None = None,
    stop_debit: float = 1.40,
    profit_frac: float = 0.50,
    now: datetime | None = None,
) -> SpxCreditAction | None:
    """Exit open credit IC when stop / profit / clock hit."""
    if str(trade.get("status") or "open").lower() != "open":
        return None
    symbol = str(trade.get("symbol") or "SPX").upper()
    entry = _f(trade.get("entry_ask") if trade.get("entry_ask") is not None else trade.get("credit"))
    debit = None
    if package and package.get("debit_to_close") is not None:
        debit = _f(package.get("debit_to_close"))
    if debit is None:
        debit = _f(trade.get("debit_to_close") if trade.get("debit_to_close") is not None else trade.get("mark"))
    stop = float(trade.get("stop_debit") or stop_debit)
    target_entry = float(entry or trade.get("target_credit") or 0.90)
    profit_debit = round(target_entry * (1.0 - float(profit_frac)), 2)

    phase = _phase(now)
    reasons: list[str] = []
    sell = False
    strength = 70.0
    if debit is not None and debit >= stop:
        sell = True
        strength = 96.0
        reasons.append(f"stop — debit ${debit:.2f} ≥ ${stop:.2f}")
    if debit is not None and entry is not None and debit <= profit_debit:
        sell = True
        strength = max(strength, 88.0)
        reasons.append(f"bank — debit ${debit:.2f} ≤ ${profit_debit:.2f} (≈{profit_frac:.0%} of credit)")
    if phase in {"final_30", "late"} and phase == "final_30":
        sell = True
        strength = max(strength, 94.0)
        reasons.append("time-stop — flatten credit IC by 15:45 ET")

    base = dict(
        symbol=symbol,
        expiry=trade.get("expiry") or (package or {}).get("expiry"),
        dte=trade.get("dte") if trade.get("dte") is not None else (package or {}).get("dte"),
        spot=_f(trade.get("spot") or (package or {}).get("spot")),
        wing_width=float(trade.get("wing_width") or (package or {}).get("wing_width") or 10),
        credit=entry,
        debit_to_close=debit,
        stop_debit=stop,
        target_credit=target_entry,
        profit_take_debit=profit_debit,
        max_loss=_f(trade.get("max_loss")),
        short_put=_f(trade.get("short_put") or (package or {}).get("short_put")),
        long_put=_f(trade.get("long_put") or (package or {}).get("long_put")),
        short_call=_f(trade.get("short_call") or (package or {}).get("short_call")),
        long_call=_f(trade.get("long_call") or (package or {}).get("long_call")),
        legs=list((package or {}).get("legs") or trade.get("legs") or []),
        contract=trade.get("contract") or (package or {}).get("contract"),
        ask=debit,
        bid=debit,
        entry_ask=entry,
        exit_plan=(
            f"BUY TO CLOSE · stop ${stop:.2f} · bank ≤${profit_debit:.2f} · flatten 15:45 ET"
        ),
    )

    if sell:
        pnl = None
        if entry is not None and debit is not None:
            pnl = round((entry - debit) * 100.0, 2)
        detail = "; ".join(reasons) or "Exit credit IC"
        if pnl is not None:
            detail += f" · P&L ~${pnl:+.2f}/ct (credit ${entry:.2f} → debit ${debit:.2f})"
        return SpxCreditAction(
            action="BUY_TO_CLOSE",
            strength=strength,
            headline=f"BUY TO CLOSE {symbol} IC",
            detail=detail,
            playbook=["credit_exit", "defined_risk_credit"],
            confirms=len(reasons),
            **base,
        )

    if entry is not None:
        hold_detail = (
            f"Open credit IC — mark debit {debit if debit is not None else '—'} · "
            f"entry ${entry:.2f} · stop ${stop:.2f} · bank ≤${profit_debit:.2f}"
        )
    else:
        hold_detail = (
            f"Open credit IC · stop ${stop:.2f} · bank ≤${profit_debit:.2f}"
        )
    return SpxCreditAction(
        action="HOLD",
        strength=60.0,
        headline=f"HOLD {symbol} IC",
        detail=hold_detail,
        playbook=["position_management"],
        confirms=1,
        **base,
    )


def fetch_spx_chain(
    symbol: str = "SPX",
    *,
    expiry: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch 0DTE (or nearest) SPX/XSP chain via Tradier when configured."""
    from odte_scanner.data.tradier import (
        access_token_from_env,
        fetch_expirations,
        fetch_option_chain,
        fetch_quotes,
    )

    sym = str(symbol).upper()
    token = access_token_from_env()
    if not token:
        return {"ok": False, "error": "tradier_not_configured", "options": [], "spot": None}

    q = fetch_quotes([sym], token=token)
    quotes = (q or {}).get("quotes") or {}
    spot = _f((quotes.get(sym) or {}).get("last") or (quotes.get(sym) or {}).get("bid"))
    today = _today_et(now).isoformat()
    exp = expiry
    if not exp:
        ex = fetch_expirations(sym, token=token)
        dates = list(ex.get("dates") or [])
        # Prefer today (0DTE), else nearest upcoming
        if today in dates:
            exp = today
        else:
            future = [d for d in dates if str(d) >= today]
            exp = future[0] if future else (dates[0] if dates else None)
    if not exp:
        return {"ok": False, "error": "no_expiry", "options": [], "spot": spot}

    chain = fetch_option_chain(sym, exp, token=token, greeks=True)
    opts = list(chain.get("options") or [])
    dte = 0
    try:
        dte = (date.fromisoformat(str(exp)[:10]) - _today_et(now)).days
    except Exception:  # noqa: BLE001
        dte = 0
    return {
        "ok": bool(chain.get("ok")),
        "symbol": sym,
        "expiry": exp,
        "dte": max(0, dte),
        "spot": spot,
        "options": opts,
        "error": chain.get("error"),
    }


def build_spx_credit_board(
    *,
    symbols: list[str] | tuple[str, ...] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    chains: dict[str, list[dict[str, Any]]] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    wing_width: float = 10.0,
    short_otm_pts: float = 50.0,
    target_credit: float = 0.90,
    min_credit: float = 0.70,
    stop_debit: float = 1.40,
    max_dte: int = 1,
    now: datetime | None = None,
    signal_store_path: str | None = None,
    fetch_live: bool = True,
) -> dict[str, Any]:
    """Build SPX/XSP credit IC board: sell_credit / wait / exit_now / hold."""
    syms = [str(s).upper() for s in (symbols or DEFAULT_SYMBOLS)]
    quotes = quotes or {}
    chains = dict(chains or {})
    open_trades = list(open_trades or [])
    store = load_signal_store(signal_store_path) if signal_store_path else {}

    sell_credit: list[dict[str, Any]] = []
    wait: list[dict[str, Any]] = []
    exit_now: list[dict[str, Any]] = []
    hold: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    errors: list[str] = []

    open_by_sym: dict[str, list[dict[str, Any]]] = {}
    open_contracts: set[str] = set()
    for t in open_trades:
        if not isinstance(t, dict):
            continue
        if str(t.get("status") or "open").lower() != "open":
            continue
        if str(t.get("structure") or t.get("lane") or "").lower() not in {
            "iron_condor",
            "spx_credit",
            "credit_ic",
            "",
        }:
            # Allow explicit spx credit trades; skip unrelated opens
            if str(t.get("lane") or "") not in {"spx_credit", ""}:
                continue
        sym = str(t.get("symbol") or "").upper()
        if not sym:
            continue
        open_by_sym.setdefault(sym, []).append(t)
        if t.get("contract"):
            open_contracts.add(str(t["contract"]))

    for sym in syms:
        spot = _f((quotes.get(sym) or {}).get("last"))
        chain = chains.get(sym)
        expiry = None
        dte = 0
        if chain is None and fetch_live:
            try:
                fetched = fetch_spx_chain(sym, now=now)
                if not fetched.get("ok"):
                    errors.append(f"{sym}: {fetched.get('error') or 'chain_failed'}")
                    continue
                chain = list(fetched.get("options") or [])
                spot = spot or _f(fetched.get("spot"))
                expiry = fetched.get("expiry")
                dte = int(fetched.get("dte") or 0)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sym}: {exc}")
                continue
        if not chain or not spot:
            errors.append(f"{sym}: missing chain/spot")
            continue
        if expiry is None:
            # Infer expiry from first option
            for o in chain:
                if isinstance(o, dict) and o.get("expiration_date"):
                    expiry = str(o["expiration_date"])[:10]
                    break
                if isinstance(o, dict) and o.get("expiry"):
                    expiry = str(o["expiry"])[:10]
                    break
        if not expiry:
            expiry = _today_et(now).isoformat()
        try:
            dte = max(0, (date.fromisoformat(str(expiry)[:10]) - _today_et(now)).days)
        except Exception:  # noqa: BLE001
            dte = int(dte or 0)
        if dte > int(max_dte):
            errors.append(f"{sym}: dte {dte} > max {max_dte}")
            continue

        tgt = target_credit if sym != "XSP" else target_credit / 10.0
        mn = min_credit if sym != "XSP" else min_credit / 10.0
        pkg = pick_iron_condor(
            chain,
            symbol=sym,
            spot=float(spot),
            expiry=str(expiry),
            dte=dte,
            wing_width=wing_width,
            short_otm_pts=short_otm_pts,
            target_credit=tgt,
            min_credit=mn,
            max_credit=(1.40 if sym != "XSP" else 0.14),
        )
        if not pkg:
            wait.append(
                SpxCreditAction(
                    action="WAIT",
                    strength=30.0,
                    headline=f"WAIT {sym} IC",
                    detail="No 10-wide IC near target credit on this chain.",
                    symbol=sym,
                    spot=float(spot),
                    expiry=str(expiry),
                    dte=dte,
                    target_credit=target_credit,
                    stop_debit=stop_debit,
                    playbook=["strike_search"],
                ).to_dict()
            )
            continue
        packages.append(pkg)

        # Exits for open trades first
        for t in open_by_sym.get(sym) or []:
            ex = decide_spx_credit_exit(
                t,
                package=pkg,
                stop_debit=stop_debit if sym != "XSP" else stop_debit / 10.0,
                now=now,
            )
            if ex is None:
                continue
            ex, store = _apply_persisted(ex, store)
            row = ex.to_dict()
            if ex.action == "BUY_TO_CLOSE":
                exit_now.append(row)
            else:
                hold.append(row)

        sig = decide_spx_credit_entry(
            pkg,
            quote=quotes.get(sym),
            target_credit=float(pkg.get("target_credit") or target_credit),
            min_credit=min_credit if sym != "XSP" else min_credit / 10.0,
            stop_debit=stop_debit if sym != "XSP" else stop_debit / 10.0,
            now=now,
            open_contracts=open_contracts,
        )
        sig, store = _apply_persisted(sig, store)
        row = sig.to_dict()
        if sig.action == "SELL_CREDIT":
            sell_credit.append(row)
        elif sig.action in {"WAIT", "SKIP"}:
            wait.append(row)
        elif sig.action == "HOLD":
            hold.append(row)

    active_keys: set[str] = set()
    for row in sell_credit + exit_now:
        act = "BUY_NOW" if row.get("action") == "SELL_CREDIT" else "SELL_NOW"
        active_keys.add(
            signal_store_key(str(row.get("symbol")), act, row.get("contract"))
        )
    if signal_store_path:
        store = prune_signal_store_to_active(store, active_keys)
        save_signal_store(signal_store_path, store)

    primary = sell_credit[0] if sell_credit else (exit_now[0] if exit_now else None)
    return {
        "label": "SPX Credit IC",
        "purpose": (
            "0DTE SPX iron condor credit — sell ~50pt OTM 10-wide wings, "
            f"target ~${target_credit:.2f} credit, stop ${stop_debit:.2f} debit-to-close."
        ),
        "lesson": (
            "SuperLuckeee-style daily SPX credit: sell put vertical + call vertical, "
            "keep premium when spot stays between short strikes; hard BTC stop at 1.40."
        ),
        "rules": [
            "SPX (or XSP) 0DTE weeklies",
            f"{wing_width:g}-wide iron condor",
            f"Shorts ~{short_otm_pts:g} pts OTM each side",
            f"Target credit ≈ ${target_credit:.2f}",
            f"Buy-to-close stop ≥ ${stop_debit:.2f}",
            "Bank ~50% of credit when available",
            "No new entries after 15:00 ET · flatten 15:45 ET",
            "Skip / WAIT on ±1.2% trend days",
        ],
        "sell_credit": sell_credit,
        "buy_now": sell_credit,  # now-board alias (enter credit)
        "wait": wait,
        "exit_now": exit_now,
        "sell_now": exit_now,  # now-board alias (close)
        "hold": hold,
        "packages": packages[:6],
        "primary": primary,
        "counts": {
            "sell_credit": len(sell_credit),
            "buy_now": len(sell_credit),
            "wait": len(wait),
            "exit_now": len(exit_now),
            "sell_now": len(exit_now),
            "hold": len(hold),
        },
        "symbols": syms,
        "target_credit": target_credit,
        "stop_debit": stop_debit,
        "wing_width": wing_width,
        "short_otm_pts": short_otm_pts,
        "errors": errors[:8],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
