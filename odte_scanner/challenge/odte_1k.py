"""0DTE $1K Challenge — Green Friday ORB15 put playbook.

Playbook (community-style, research only):
  • Green session (Green Friday) — market green, fade weakness with puts
  • Puts at open / after ORB15 forms
  • Break + hold ORB15 Low → PUT NOW
  • Retest of ORB Low from below → second entry window
  • Surface conflict when call "safe zone" / Red Flag still likes index calls
  • Paper sleeve starts $1,000; size ~85% (~$850); max 2 trades / day
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.challenge.orb15 import (
    Orb15Levels,
    classify_vs_orb,
    compute_orb15,
    fetch_orb15_bars,
)
from odte_scanner.time_cst import signal_timestamps, to_cst_label

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
FLATTEN_ET = time(15, 45)

# Always keep these near the front of the ORB fetch queue
_ORB_PRIORITY = (
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "SPX",
    "XSP",
    "TSLA",
    "NVDA",
    "AAPL",
    "NBIS",
    "SLV",
    "SPCX",
    "NOW",
    "AMZN",
    "META",
    "AMD",
    "MSFT",
    "CRWV",
    "PLTR",
    "SMCI",
)


def resolve_odte_1k_symbols(
    symbols: list[str] | str | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Resolve 0DTE $1K watchlist.

    - list → that list
    - \"focus\" / \"all\" / None → config tickers (full focus sleeve)
    Always de-dupes and keeps priority names first when present.
    """
    cfg = config or {}
    raw = symbols
    if raw is None:
        raw = (cfg.get("actions") or {}).get("odte_1k_symbols")
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in {"", "focus", "all", "tickers", "universe"}:
            raw = list(cfg.get("tickers") or [])
        else:
            raw = [raw]
    if not raw:
        raw = list(cfg.get("tickers") or _ORB_PRIORITY)

    seen: set[str] = set()
    out: list[str] = []
    for sym in list(_ORB_PRIORITY) + [str(s).upper() for s in raw]:
        key = str(sym).replace(".", "-").upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def orb_from_quote_proxy(symbol: str, quote: dict[str, Any] | None) -> Orb15Levels | None:
    """Day high/low proxy when 1m ORB15 bars were not fetched."""
    q = quote or {}
    high = q.get("day_high")
    low = q.get("day_low")
    if high is None or low is None:
        return None
    try:
        hi = float(high)
        lo = float(low)
    except Exception:  # noqa: BLE001
        return None
    if hi <= 0 or lo <= 0 or hi < lo:
        return None
    return Orb15Levels(
        symbol=str(symbol).upper(),
        session_date=datetime.now(ET).date().isoformat(),
        high=round(hi, 4),
        low=round(lo, 4),
        open=float(q["session_open"]) if q.get("session_open") is not None else None,
        bars=0,
        status="proxy",
        asof=str(q.get("asof") or datetime.now(ET).isoformat()),
        note=f"Day H/L proxy ${hi:.2f} / ${lo:.2f} (ORB15 1m not fetched)",
    )


@dataclass
class Odte1kSignal:
    action: str  # PUT_NOW | CALL_NOW | HOLD | EXIT | WAIT | WATCH
    symbol: str
    right: str  # P | C
    strength: float
    headline: str
    detail: str
    playbook: list[str] = field(default_factory=list)
    orb_high: float | None = None
    orb_low: float | None = None
    spot: float | None = None
    green_friday: bool = False
    broke_orb_low: bool = False
    holds_below_low: bool = False
    retest_orb_low: bool = False
    call_safe_zone_conflict: bool = False
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    contract: str | None = None
    dte: int | None = 0
    dte_bucket: str = "0dte"
    position_size_usd: float | None = None
    contracts: int | None = None
    signaled_at: str | None = None
    signaled_at_cst: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons or [])
        d["playbook"] = list(self.playbook or [])
        if self.action in {"PUT_NOW", "CALL_NOW", "EXIT"} and not d.get("signaled_at"):
            d.update(signal_timestamps())
        return d


def _session_green(quote: dict[str, Any] | None) -> bool:
    if not quote:
        return False
    for k in ("session_change_pct", "change_pct"):
        if quote.get(k) is not None:
            return float(quote[k]) > 0
    return False


def _call_safe_zone(red_flag: dict[str, Any] | None, actions: dict[str, Any] | None) -> tuple[bool, str]:
    """True when desks still push index calls while we want puts."""
    notes: list[str] = []
    conflict = False
    rf = red_flag or {}
    state = str(rf.get("state") or "")
    if state == "SUPPORTIVE" and not rf.get("block_0dte_long_calls"):
        conflict = True
        notes.append(f"Red Flag {state} — call safe zone still active")
    elif state and state != "RED_FLAG" and not rf.get("block_0dte_long_calls"):
        notes.append(f"Red Flag {state} — not blocking index calls")

    acts = actions or {}
    for row in (acts.get("buy_now_0dte") or acts.get("buy_now_calls") or acts.get("buy_now") or [])[:6]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        right = str(row.get("right") or "C").upper()
        if sym in {"SPY", "QQQ", "SPX", "IWM"} and right != "P":
            conflict = True
            notes.append(f"{sym} CALL still on BUY NOW ({row.get('action')})")
            break
    return conflict, " · ".join(notes[:3])


def _suggest_put_zone(spot: float) -> dict[str, Any]:
    """ATM-ish put zone when live chain unavailable (Pages / offline)."""
    strike = round(spot)  # SPY often $1 strikes
    today = datetime.now(ET).date()
    return {
        "strike": float(strike),
        "expiry": today.isoformat(),
        "dte": 0,
        "ask": None,
        "bid": None,
        "contract": None,
        "mark_source": "zone",
    }


def _pick_0dte_put(symbol: str, spot: float, *, yahoo_symbol: str | None = None) -> dict[str, Any] | None:
    try:
        from odte_scanner.options.selector import select_puts

        puts = select_puts(
            symbol,
            spot,
            score=70.0,
            reasons=["0DTE 1K ORB15 put"],
            max_dte=1,
            odte_max_dte=1,
            otm_pct_max=1.5,
            itm_pct_max=0.5,
            max_ask=12.0,
            min_open_interest=100,
            min_volume=50,
            yahoo_symbol=yahoo_symbol,
            per_bucket=1,
        )
        if not puts:
            return None
        p = puts[0]
        d = p.to_dict() if hasattr(p, "to_dict") else dict(p)
        return {
            "strike": d.get("strike"),
            "expiry": d.get("expiry"),
            "dte": d.get("dte") if d.get("dte") is not None else 0,
            "ask": d.get("ask"),
            "bid": d.get("bid"),
            "contract": d.get("contract"),
            "mark_source": "ask",
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("0dte put pick %s: %s", symbol, exc)
        return None


def _contracts_for_size(ask: float | None, size_usd: float, cash: float) -> int:
    if ask is None or ask <= 0:
        return 0
    budget = min(float(size_usd), float(cash))
    n = int(budget // (ask * 100))
    return max(0, n)


def decide_odte_1k_entry(
    *,
    orb: Orb15Levels,
    quote: dict[str, Any] | None,
    symbol: str = "SPY",
    red_flag: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    position_size_usd: float = 850.0,
    cash: float = 1000.0,
    trades_today: int = 0,
    max_trades_per_day: int = 2,
    open_trade: dict[str, Any] | None = None,
    fetch_contract: bool = False,
    yahoo_symbol: str | None = None,
    buffer_usd: float = 0.05,
    retest_band_usd: float = 0.40,
    flatten_et: str = "15:45",
    now: datetime | None = None,
) -> Odte1kSignal:
    """Core PUT NOW / WAIT / HOLD / EXIT decision for the 0DTE 1K sleeve."""
    now_et = now.astimezone(ET) if now and now.tzinfo else (now.replace(tzinfo=ET) if now else datetime.now(ET))
    try:
        hh, mm = [int(x) for x in str(flatten_et).split(":")[:2]]
        flat_t = time(hh, mm)
    except Exception:  # noqa: BLE001
        flat_t = FLATTEN_ET

    q = quote or {}
    last = q.get("last")
    if last is None and q.get("live_last") is not None:
        last = q.get("live_last")
    last_f = float(last) if last is not None else None
    green = _session_green(q)
    vs = classify_vs_orb(last_f, orb, buffer_usd=buffer_usd, retest_band_usd=retest_band_usd)
    conflict, conflict_note = _call_safe_zone(red_flag, actions)
    playbook = ["0dte_1k", "orb15", "green_friday_puts" if green else "session_fade"]

    # Open trade management first
    if open_trade and str(open_trade.get("status") or "open") == "open":
        entry = float(open_trade.get("entry_ask") or 0)
        mark = open_trade.get("mark") or open_trade.get("bid") or open_trade.get("ask")
        unreal = None
        if mark is not None and entry > 0:
            unreal = (float(mark) - entry) / entry * 100.0
        action = "HOLD"
        detail = f"Open PUT — mark ${float(mark):.2f}" if mark is not None else "Open PUT — holding"
        strength = 55.0
        if unreal is not None and unreal >= 80:
            action = "EXIT"
            detail = f"EXIT — bank +{unreal:.0f}% premium"
            strength = 90.0
        elif unreal is not None and unreal <= -45:
            action = "EXIT"
            detail = f"EXIT — stop {unreal:.0f}%"
            strength = 85.0
        elif now_et.time() >= flat_t:
            action = "EXIT"
            detail = f"EXIT — 0DTE flatten by {flatten_et} ET"
            strength = 88.0
        elif vs.get("above_orb_high") or (orb.low is not None and last_f is not None and last_f > float(orb.low) + 0.35):
            action = "EXIT"
            detail = "EXIT — reclaim above ORB Low / range fail"
            strength = 80.0
        sig = Odte1kSignal(
            action=action,
            symbol=symbol,
            right="P",
            strength=strength,
            headline=f"{action} {symbol} PUT",
            detail=detail,
            playbook=playbook + ["manage_open"],
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            broke_orb_low=bool(vs.get("broke_orb_low")),
            holds_below_low=bool(vs.get("holds_below_low")),
            retest_orb_low=bool(vs.get("retest_orb_low")),
            call_safe_zone_conflict=conflict,
            strike=open_trade.get("strike"),
            expiry=open_trade.get("expiry"),
            ask=open_trade.get("ask") or open_trade.get("entry_ask"),
            bid=open_trade.get("bid") or open_trade.get("mark"),
            contract=open_trade.get("contract"),
            dte=0,
            position_size_usd=float(open_trade.get("cost") or position_size_usd),
            contracts=int(open_trade.get("contracts") or 1),
            reasons=[detail] + ([conflict_note] if conflict_note else []),
        )
        if action == "EXIT":
            ts = signal_timestamps()
            sig.signaled_at = ts["signaled_at"]
            sig.signaled_at_cst = ts["signaled_at_cst"]
        return sig

    # Pre / forming ORB
    if orb.status == "forming":
        return Odte1kSignal(
            action="WAIT",
            symbol=symbol,
            right="P",
            strength=35.0,
            headline=f"WAIT ORB15 {symbol}",
            detail=orb.note or "ORB15 still forming (09:30–09:45 ET)",
            playbook=playbook,
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            call_safe_zone_conflict=conflict,
            reasons=[orb.note or "forming", "Puts preferred after ORB Low prints"],
        )

    if orb.status not in {"ready", "proxy"} or orb.low is None:
        return Odte1kSignal(
            action="WAIT",
            symbol=symbol,
            right="P",
            strength=20.0,
            headline=f"WAIT ORB15 {symbol}",
            detail=orb.note or "ORB15 levels unavailable",
            playbook=playbook,
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            call_safe_zone_conflict=conflict,
            reasons=[orb.note or "no ORB"],
        )

    if trades_today >= max_trades_per_day:
        return Odte1kSignal(
            action="WAIT",
            symbol=symbol,
            right="P",
            strength=40.0,
            headline=f"WAIT {symbol} — day cap",
            detail=f"Max {max_trades_per_day} trades/day hit ({trades_today})",
            playbook=playbook,
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            call_safe_zone_conflict=conflict,
            reasons=[f"day trade cap {trades_today}/{max_trades_per_day}"],
        )

    if now_et.time() >= flat_t:
        return Odte1kSignal(
            action="WAIT",
            symbol=symbol,
            right="P",
            strength=25.0,
            headline=f"WAIT {symbol} — past flatten",
            detail=f"No new 0DTE entries after {flatten_et} ET",
            playbook=playbook,
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            call_safe_zone_conflict=conflict,
            reasons=["past flatten clock"],
        )

    broke = bool(vs.get("broke_orb_low"))
    holds = bool(vs.get("holds_below_low"))
    retest = bool(vs.get("retest_orb_low"))
    mom5 = q.get("mom_5m_pct")
    tape_ok = mom5 is None or float(mom5) <= 0.08

    put_trigger = False
    trigger_why: list[str] = []
    if broke and holds and tape_ok:
        put_trigger = True
        trigger_why.append(f"Break + hold ORB Low ${orb.low:.2f}")
    elif retest and (broke or holds) and tape_ok:
        put_trigger = True
        trigger_why.append(f"Retest ORB Low ${orb.low:.2f} from below")
    elif holds and green and tape_ok and last_f is not None and last_f <= float(orb.low) + buffer_usd:
        # Soft: green Friday + sitting on ORB Low
        put_trigger = True
        trigger_why.append(f"Green Friday · hold ORB Low ${orb.low:.2f}")

    if green:
        trigger_why.insert(0, "Green Friday ✅")

    if not put_trigger:
        return Odte1kSignal(
            action="WATCH",
            symbol=symbol,
            right="P",
            strength=45.0,
            headline=f"WATCH {symbol} ORB Low",
            detail=(
                f"ORB Low ${orb.low:.2f} / High ${orb.high:.2f} · spot "
                f"{'—' if last_f is None else f'${last_f:.2f}'} — wait break+hold or retest"
            ),
            playbook=playbook,
            orb_high=orb.high,
            orb_low=orb.low,
            spot=last_f,
            green_friday=green,
            broke_orb_low=broke,
            holds_below_low=holds,
            retest_orb_low=retest,
            call_safe_zone_conflict=conflict,
            reasons=trigger_why
            + [
                f"Need break+hold below ${orb.low:.2f} or retest from below",
                conflict_note or "No call conflict noted",
            ],
        )

    # PUT NOW — attach contract when possible
    contract = None
    if fetch_contract and last_f:
        contract = _pick_0dte_put(symbol, last_f, yahoo_symbol=yahoo_symbol)
    if contract is None and last_f:
        contract = _suggest_put_zone(last_f)

    ask = float(contract["ask"]) if contract and contract.get("ask") is not None else None
    n_ct = _contracts_for_size(ask, position_size_usd, cash) if ask else None
    detail_bits = list(trigger_why)
    if conflict:
        detail_bits.append(f"⚠️ Call safe-zone conflict: {conflict_note}")
    if orb.low is not None:
        detail_bits.append(f"Invalidation: reclaim > ORB Low ${orb.low:.2f} + $0.35")
    detail_bits.append(f"Size ~${position_size_usd:.0f} · max {max_trades_per_day} trades/day")

    ts = signal_timestamps()
    return Odte1kSignal(
        action="PUT_NOW",
        symbol=symbol,
        right="P",
        strength=82.0 if broke else 74.0,
        headline=f"PUT NOW {symbol}",
        detail=" · ".join(detail_bits),
        playbook=playbook + (["retest_entry"] if retest and not broke else ["break_hold"]),
        orb_high=orb.high,
        orb_low=orb.low,
        spot=last_f,
        green_friday=green,
        broke_orb_low=broke,
        holds_below_low=holds,
        retest_orb_low=retest,
        call_safe_zone_conflict=conflict,
        strike=(contract or {}).get("strike"),
        expiry=(contract or {}).get("expiry"),
        ask=ask,
        bid=float(contract["bid"]) if contract and contract.get("bid") is not None else None,
        contract=(contract or {}).get("contract"),
        dte=int((contract or {}).get("dte") or 0),
        position_size_usd=position_size_usd,
        contracts=n_ct,
        signaled_at=ts["signaled_at"],
        signaled_at_cst=ts["signaled_at_cst"],
        reasons=detail_bits,
    )


def build_odte_1k_board(
    *,
    quotes: dict[str, dict[str, Any]] | None = None,
    red_flag: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    symbols: list[str] | str | None = None,
    config: dict[str, Any] | None = None,
    orb_map: dict[str, Orb15Levels] | None = None,
    open_trades: list[dict[str, Any]] | None = None,
    book: dict[str, Any] | None = None,
    starting_cash: float = 1000.0,
    position_size_usd: float = 850.0,
    position_pct: float | None = 0.85,
    max_trades_per_day: int = 2,
    max_orb_fetch: int = 20,
    max_contract_fetch: int = 8,
    fetch_bars: bool = True,
    fetch_contracts: bool = False,
    flatten_et: str = "15:45",
    aliases: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the 0DTE $1K Challenge board (full focus sleeve; capped live ORB fetches)."""
    quotes = quotes or {}
    aliases = aliases or {}
    symbols = resolve_odte_1k_symbols(symbols, config=config)
    open_trades = open_trades or []
    book = book or {}
    cash = float(book.get("cash") if book.get("cash") is not None else starting_cash)
    equity = float(book.get("equity") if book.get("equity") is not None else cash)
    if position_pct is not None and position_pct > 0:
        size = round(min(float(position_size_usd), equity * float(position_pct)), 2)
    else:
        size = float(position_size_usd)

    # Count trades opened today (ET)
    now_et = now.astimezone(ET) if now and now.tzinfo else datetime.now(ET)
    today = now_et.date().isoformat()
    trades_today = 0
    for t in open_trades + list(book.get("trades") or []):
        entered = str(t.get("entered_at") or "")
        if entered.startswith(today) or (entered and to_cst_label(entered) and today in entered[:10]):
            trades_today += 1
        else:
            try:
                dt = datetime.fromisoformat(entered.replace("Z", "+00:00")).astimezone(ET)
                if dt.date().isoformat() == today:
                    trades_today += 1
            except Exception:  # noqa: BLE001
                pass
    # Prefer explicit counter from book
    if book.get("trades_today") is not None:
        trades_today = int(book["trades_today"])

    open_by_sym = {
        str(t.get("symbol")).upper(): t
        for t in open_trades
        if str(t.get("status") or "open") == "open"
    }

    # Rank which names get expensive 1m ORB fetches (priority + weakest tape first)
    def _tape_rank(sym: str) -> tuple:
        q = quotes.get(sym) or {}
        live = q.get("session_change_pct")
        if live is None:
            live = q.get("change_pct")
        mom = q.get("mom_5m_pct")
        # Lower = fetch sooner (priority index, then weakest tape)
        try:
            pri = list(_ORB_PRIORITY).index(sym)
        except ValueError:
            pri = 100 + symbols.index(sym) if sym in symbols else 999
        weak = float(live) if live is not None else 0.0
        mom_f = float(mom) if mom is not None else 0.0
        return (0 if sym in open_by_sym else 1, pri, weak, mom_f)

    fetch_queue = sorted(symbols, key=_tape_rank)[: max(1, int(max_orb_fetch))]
    fetch_set = set(fetch_queue) if fetch_bars else set()

    levels: dict[str, Any] = {}
    signals: list[Odte1kSignal] = []
    contract_fetches = 0
    for sym in symbols:
        orb = (orb_map or {}).get(sym)
        if orb is None and sym in fetch_set:
            bars = fetch_orb15_bars(sym, yahoo_symbol=aliases.get(sym))
            orb = compute_orb15(bars, symbol=sym, now=now)
        if orb is None:
            orb = orb_from_quote_proxy(sym, quotes.get(sym))
        if orb is None:
            orb = Orb15Levels(
                symbol=sym,
                session_date=now_et.date().isoformat(),
                status="incomplete",
                note="No ORB15 bars / day range yet",
            )
        elif not isinstance(orb, Orb15Levels):
            orb = Orb15Levels(
                symbol=str(orb.get("symbol") or sym),
                session_date=str(orb.get("session_date") or ""),
                high=orb.get("high"),
                low=orb.get("low"),
                open=orb.get("open"),
                close_at_0950=orb.get("close_at_0950"),
                bars=int(orb.get("bars") or 0),
                status=str(orb.get("status") or "incomplete"),
                asof=orb.get("asof"),
                note=orb.get("note"),
            )
        levels[sym] = orb.to_dict()
        # First pass without contracts — attach live asks only for top PUT_NOW after rank
        sig = decide_odte_1k_entry(
            orb=orb,
            quote=quotes.get(sym),
            symbol=sym,
            red_flag=red_flag,
            actions=actions,
            position_size_usd=size,
            cash=cash,
            trades_today=trades_today,
            max_trades_per_day=max_trades_per_day,
            open_trade=open_by_sym.get(sym),
            fetch_contract=False,
            yahoo_symbol=aliases.get(sym),
            flatten_et=flatten_et,
            now=now,
        )
        signals.append(sig)

    # Attach live 0DTE puts for top PUT_NOW / EXIT names (capped)
    if fetch_contracts:
        for sig in sorted(signals, key=lambda s: (0 if s.action in {"PUT_NOW", "EXIT"} else 1, -s.strength)):
            if sig.action not in {"PUT_NOW", "EXIT", "HOLD"}:
                continue
            if contract_fetches >= int(max_contract_fetch):
                break
            if sig.ask is not None and sig.contract:
                continue
            last = sig.spot or (quotes.get(sig.symbol) or {}).get("last")
            if not last:
                continue
            contract = _pick_0dte_put(sig.symbol, float(last), yahoo_symbol=aliases.get(sig.symbol))
            contract_fetches += 1
            if not contract:
                continue
            sig.strike = contract.get("strike")
            sig.expiry = contract.get("expiry")
            sig.dte = int(contract.get("dte") or 0)
            sig.ask = float(contract["ask"]) if contract.get("ask") is not None else None
            sig.bid = float(contract["bid"]) if contract.get("bid") is not None else None
            sig.contract = contract.get("contract")
            if sig.ask:
                sig.contracts = _contracts_for_size(sig.ask, size, cash) or None

    puts = [s for s in signals if s.action == "PUT_NOW"]
    exits = [s for s in signals if s.action == "EXIT"]
    holds = [s for s in signals if s.action == "HOLD"]
    watches = [s for s in signals if s.action in {"WATCH", "WAIT"}]
    primary = (exits + puts + holds + watches)[0] if signals else None

    green = any(s.green_friday for s in signals)
    conflict = any(s.call_safe_zone_conflict for s in signals)

    return {
        "desk": "odte_1k",
        "title": "0DTE $1K Challenge",
        "generated_at": datetime.now(ET).isoformat(),
        "starting_cash": starting_cash,
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "progress_2x_pct": round((equity / (starting_cash * 2)) * 100.0, 2) if starting_cash else 0,
        "doubled": equity >= starting_cash * 2,
        "position_size_usd": size,
        "position_pct": position_pct,
        "max_trades_per_day": max_trades_per_day,
        "trades_today": trades_today,
        "green_friday": green,
        "call_safe_zone_conflict": conflict,
        "symbols": symbols,
        "orb_fetched": sorted(fetch_set),
        "orb": levels,
        "primary": primary.to_dict() if primary else None,
        "put_now": [s.to_dict() for s in puts],
        "exit_now": [s.to_dict() for s in exits],
        "hold": [s.to_dict() for s in holds],
        "watch": [s.to_dict() for s in watches],
        "all": [s.to_dict() for s in signals],
        "counts": {
            "put_now": len(puts),
            "exit_now": len(exits),
            "hold": len(holds),
            "watch": len(watches),
            "trades_today": trades_today,
            "names": len(symbols),
            "orb_ready": sum(1 for v in levels.values() if v.get("status") in {"ready", "proxy"}),
            "orb_fetched": len(fetch_set),
        },
        "book": book,
        "playbook": [
            "Green Friday: session green — prefer puts on ORB weakness (not chase calls).",
            "ORB15 = first 15m RTH High/Low (09:30–09:45 ET). Full focus sleeve (SPY/QQQ/IWM/TSLA/NVDA/NBIS/AAPL/SLV/SPCX/NOW/…). ",
            "PUT NOW on break + hold of ORB Low, or retest of ORB Low from below.",
            "Size ~85% of sleeve (~$850 on $1k). Max 2 trades / day.",
            "Flatten by 15:45 ET. Invalidation = reclaim above ORB Low.",
            "If call safe-zone / Red Flag SUPPORTIVE still shows SPY calls — treat as conflict, not confirmation.",
        ],
        "disclaimer": (
            "Educational / research only. Models ORB15 from delayed Yahoo 1m bars "
            "(capped live fetch; others use day H/L proxy). "
            "Not affiliated with any Discord. Options can expire worthless. "
            "PUT NOW is the actionable entry — Paper ENTER on a live host."
        ),
    }
