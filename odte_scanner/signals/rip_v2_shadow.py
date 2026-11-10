"""RIP-v2 shadow mode. Logs a hypothetical trade next to every live RIP signal.

Shadow only: this module never places, sizes, cancels, mutes, or changes a
live RIP alert or a Webull paper order. It reads the RIP board the scan
already built, reads live option quotes, and writes its own state files.

RIP-v2 rules (candidate from the Nov research backtest):

* Strike ATM to at most 1.0% OTM (ITM no deeper than the live 0.35% cap).
  The live contract is reused when it already fits; otherwise the chain is
  re-picked with a 1.0% cap. A weekly fallback is allowed only when it also
  fits the 1.0% cap.
* Entry at that contract's live ask.
* Exits: +50% target, −35% stop, trail after +30% when the gain gives back
  half the peak, flat by 2:45 PM CT. Never held overnight.
* Never rebuy an OCC that lost (live loss cooldown plus v2's own losers).

"Current" stats come from Webull paper fills (the source of truth) on the
contracts the live RIP lane signaled since the shadow start day. Both sides
are shown per 1 contract so they compare like for like.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]
DAY_STORE = ROOT / "outputs" / "rip_v2_shadow_day.json"
HISTORY_STORE = ROOT / "outputs" / "rip_v2_shadow.jsonl"

MAX_OTM_PCT = 1.0
MAX_ITM_PCT = 0.35
TAKE_PROFIT_PCT = 50.0
STOP_LOSS_PCT = 35.0
TRAIL_ARM_PCT = 30.0
TRAIL_GIVEBACK = 0.5
OPEN_CT = dtime(8, 30)
FLAT_CT = dtime(14, 45)
MAX_WEEKLY_DTE = 14
MARK_MIN_GAP = timedelta(seconds=20)

LABEL = "RIP-v2 shadow"
NOTE = (
    "Shadow · not traded. A hypothetical RIP-v2 trade is logged next to each live "
    "RIP signal: strike ATM to ≤1% OTM, entry at the live Ask, +50% target, −35% stop, "
    "trail at half of peak after +30%, flat by 2:45 PM CT, no overnight. "
    "Live RIP alerts and paper trades are unchanged. Stats are per 1 contract."
)
RULES = {
    "max_otm_pct": MAX_OTM_PCT,
    "max_itm_pct": MAX_ITM_PCT,
    "take_profit_pct": TAKE_PROFIT_PCT,
    "stop_loss_pct": STOP_LOSS_PCT,
    "trail_arm_pct": TRAIL_ARM_PCT,
    "trail_giveback": TRAIL_GIVEBACK,
    "flat_ct": "14:45",
    "overnight": False,
    "never_rebuy_losing_occ": True,
}

_LOCK = threading.RLock()


# ---------------------------------------------------------------- helpers


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _ct(moment: datetime | None = None) -> datetime:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CT)


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ct(moment)


def _hm(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}"


def money_label(kind: str, value: Any) -> str | None:
    """'Ask $1.23', 'Bid $0.95'. Every price shown carries its label."""
    number = _num(value)
    if number is None:
        return None
    return f"{kind} ${number:.2f}"


def moneyness_pct(strike: Any, spot: Any) -> float | None:
    k = _num(strike)
    s = _num(spot)
    if k is None or s is None or s <= 0:
        return None
    return (k - s) / s * 100.0


def fits_v2(mny: float | None, *, max_otm: float = MAX_OTM_PCT, max_itm: float = MAX_ITM_PCT) -> bool:
    return mny is not None and -max_itm - 1e-9 <= float(mny) <= max_otm + 1e-9


def _dte(expiry: Any, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - today).days
    except ValueError:
        return None


# ---------------------------------------------------------------- state


def read_day(path: Path | None = None) -> dict[str, Any]:
    from odte_scanner.json_util import load_json_state

    blob = load_json_state(path or DAY_STORE)
    return blob if isinstance(blob, dict) else {}


def write_day(state: dict[str, Any], path: Path | None = None) -> None:
    from odte_scanner.json_util import atomic_write_text, dumps_strict

    atomic_write_text(path or DAY_STORE, dumps_strict(state, indent=2, default=str))


def read_history(path: Path | None = None) -> list[dict[str, Any]]:
    dest = path or HISTORY_STORE
    rows: list[dict[str, Any]] = []
    try:
        with dest.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _append_history(record: dict[str, Any], path: Path | None = None) -> None:
    from odte_scanner.json_util import dumps_strict

    dest = path or HISTORY_STORE
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("a", encoding="utf-8") as handle:
        handle.write(dumps_strict(record, default=str) + "\n")


def _start_day(state: dict[str, Any], history: list[dict[str, Any]], today: str) -> str:
    days = [str(r.get("day")) for r in history if r.get("day")]
    if state.get("start_day"):
        days.append(str(state["start_day"]))
    return min(days) if days else today


# ---------------------------------------------------------------- choosing


def _default_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    from odte_scanner.options.contract_quote import fetch_contract_quote

    return fetch_contract_quote(row)


def _default_pick(symbol: str, spot: float, blocked: set[str]) -> dict[str, Any] | None:
    from odte_scanner.signals.rip_radar import pick_rip_continuation_call

    return pick_rip_continuation_call(
        symbol,
        float(spot),
        max_weekly_dte=MAX_WEEKLY_DTE,
        max_otm_pct=MAX_OTM_PCT,
        max_itm_pct=MAX_ITM_PCT,
        prefer_otm=True,
        loss_cooldown_contracts=blocked,
    )


def choose_v2_contract(
    ticket: dict[str, Any],
    *,
    blocked: set[str],
    quote: Callable[[dict[str, Any]], dict[str, Any] | None],
    pick: Callable[[str, float, set[str]], dict[str, Any] | None],
    today: date,
) -> tuple[dict[str, Any] | None, str]:
    """(contract, why). contract is None when v2 takes no trade."""
    symbol = str(ticket.get("symbol") or "").upper()
    spot = _num(ticket.get("spot"))
    live_contract = str(ticket.get("contract") or "").upper()
    mny = _num(ticket.get("moneyness_pct"))
    if mny is None:
        mny = moneyness_pct(ticket.get("strike"), spot)
    if str(ticket.get("right") or "C").upper() != "C":
        return None, "RIP-v2 is calls only"
    if live_contract and fits_v2(mny) and live_contract not in blocked:
        live = quote(
            {
                "symbol": symbol,
                "contract": live_contract,
                "expiry": ticket.get("expiry"),
                "strike": ticket.get("strike"),
                "right": "C",
            }
        ) or {}
        ask = _num(live.get("ask"))
        source = live.get("source") or "live"
        if ask is None:
            ask = _num(ticket.get("ask"))
            source = "signal ask"
        if ask is None or ask <= 0:
            return None, "no live Ask on the live contract"
        dte = _dte(ticket.get("expiry"), today)
        return (
            {
                "contract": live_contract,
                "strike": _num(ticket.get("strike")),
                "expiry": str(ticket.get("expiry") or "")[:10] or None,
                "dte": dte,
                "moneyness_pct": round(float(mny), 2),
                "ask": ask,
                "bid": _num(live.get("bid")),
                "quote_source": source,
                "same_as_live": True,
                "weekly_fallback": bool(dte is not None and dte > 1),
            },
            "live contract fits ATM–1% OTM",
        )
    if spot is None or spot <= 0:
        return None, "no spot price to re-pick a strike"
    picked = pick(symbol, float(spot), blocked)
    if not picked:
        why = "no listed call ATM to ≤1% OTM with a live Ask"
        if live_contract in blocked:
            why = "live OCC lost before (never rebuy) and no other call ATM to ≤1% OTM"
        return None, why
    contract = str(picked.get("contract") or "").upper()
    if contract in blocked:
        return None, "only fitting OCC lost before (never rebuy)"
    picked_mny = _num(picked.get("moneyness_pct"))
    if picked_mny is None:
        picked_mny = moneyness_pct(picked.get("strike"), spot)
    if not fits_v2(picked_mny):
        return None, "re-picked strike is outside ATM to ≤1% OTM"
    ask = _num(picked.get("ask"))
    if ask is None or ask <= 0:
        return None, "re-picked contract has no live Ask"
    dte = _num(picked.get("dte"))
    if dte is None:
        dte = _dte(picked.get("expiry"), today)
    return (
        {
            "contract": contract or None,
            "strike": _num(picked.get("strike")),
            "expiry": str(picked.get("expiry") or "")[:10] or None,
            "dte": None if dte is None else int(dte),
            "moneyness_pct": round(float(picked_mny), 2),
            "ask": ask,
            "bid": _num(picked.get("bid")),
            "quote_source": picked.get("source") or "chain",
            "same_as_live": contract == live_contract,
            "weekly_fallback": bool(picked.get("weekly_fallback")),
        },
        "re-picked ATM to ≤1% OTM" + (" (weekly fallback)" if picked.get("weekly_fallback") else ""),
    )


def signal_key(ticket: dict[str, Any]) -> str:
    contract = str(ticket.get("contract") or "").upper()
    if contract:
        return contract
    return f"{str(ticket.get('symbol') or '').upper()}|{ticket.get('signaled_at') or ''}"


# ---------------------------------------------------------------- exits


def exit_decision(gain_pct: float, peak_pct: float, moment: datetime) -> str | None:
    """v2 exit reason for this mark, or None to keep holding."""
    if gain_pct >= TAKE_PROFIT_PCT:
        return "target +50%"
    if gain_pct <= -STOP_LOSS_PCT:
        return "stop −35%"
    if peak_pct >= TRAIL_ARM_PCT and gain_pct <= peak_pct * TRAIL_GIVEBACK:
        return "trail (gave back half of peak)"
    if _ct(moment).time() >= FLAT_CT:
        return "flat 2:45 PM CT"
    return None


def _mark_of(quote: dict[str, Any] | None) -> tuple[float | None, str]:
    """The price v2 could sell at: the live bid, else the mid."""
    q = quote or {}
    bid = _num(q.get("bid"))
    ask = _num(q.get("ask"))
    if bid is not None and bid > 0:
        return bid, "Bid"
    if ask is not None and ask > 0 and bid is not None:
        return (ask + bid) / 2.0, "Mid"
    return None, ""


def _close(trade: dict[str, Any], price: float, basis: str, reason: str, moment: datetime) -> None:
    entry = float(trade["entry_ask"])
    pct = (price / entry - 1.0) * 100.0
    trade.update(
        status="closed",
        exit_price=round(price, 4),
        exit_label=money_label(basis or "Mark", price),
        exit_at=moment.isoformat(),
        exit_ct=_hm(_ct(moment)),
        exit_reason=reason,
        pnl_pct=round(pct, 2),
        pnl_usd=round((price - entry) * 100.0, 2),
    )


def mark_open_trades(
    state: dict[str, Any],
    moment: datetime,
    *,
    quote: Callable[[dict[str, Any]], dict[str, Any] | None],
    force: bool = False,
) -> list[dict[str, Any]]:
    """Mark open v2 trades with live quotes. Returns trades closed now."""
    closed: list[dict[str, Any]] = []
    for row in state.get("signals") or []:
        trade = row.get("v2") or {}
        if trade.get("status") != "open":
            continue
        last = _when(trade.get("last_mark_at"))
        flat_due = _ct(moment).time() >= FLAT_CT
        if not force and not flat_due and last is not None and moment - last < MARK_MIN_GAP:
            continue
        try:
            q = quote(
                {
                    "symbol": row.get("symbol"),
                    "contract": trade.get("contract"),
                    "expiry": trade.get("expiry"),
                    "strike": trade.get("strike"),
                    "right": "C",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("rip v2 shadow quote failed %s: %s", trade.get("contract"), exc)
            q = None
        price, basis = _mark_of(q)
        entry = _num(trade.get("entry_ask"))
        if entry is None or entry <= 0:
            continue
        if price is not None:
            trade["last_mark"] = round(price, 4)
            trade["last_mark_label"] = money_label(basis, price)
            trade["last_mark_basis"] = basis
            trade["last_mark_at"] = moment.isoformat()
            gain = (price / entry - 1.0) * 100.0
            peak = max(float(trade.get("peak_pct") or 0.0), gain)
            trade["peak_pct"] = round(peak, 2)
            trade["gain_pct"] = round(gain, 2)
            reason = exit_decision(gain, peak, moment)
            if reason:
                _close(trade, price, basis, reason, moment)
                closed.append(row)
        elif flat_due:
            fallback = _num(trade.get("last_mark"))
            if fallback is not None:
                _close(trade, fallback, "Last mark", "flat 2:45 PM CT (no live quote; last mark)", moment)
            else:
                _close(trade, entry, "Entry Ask", "flat 2:45 PM CT (never got a live quote; scored at entry)", moment)
            closed.append(row)
    return closed


# ---------------------------------------------------------------- cycle


def _losing_occs(history: Iterable[dict[str, Any]], state: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    rows = list(history) + list(state.get("signals") or [])
    for row in rows:
        trade = row.get("v2") or {}
        if trade.get("status") == "closed" and (_num(trade.get("pnl_usd")) or 0) < 0 and trade.get("contract"):
            out.add(str(trade["contract"]).upper())
    return out


def _finalize(row: dict[str, Any], history_path: Path | None) -> None:
    if row.get("logged"):
        return
    status = (row.get("v2") or {}).get("status")
    if status not in {"closed", "no_trade"}:
        return
    record = {k: v for k, v in row.items() if k != "logged"}
    _append_history(record, history_path)
    row["logged"] = True


def _rollover(state: dict[str, Any], history_path: Path | None) -> None:
    """Close leftovers from a previous day at their last mark and log them."""
    for row in state.get("signals") or []:
        trade = row.get("v2") or {}
        if trade.get("status") == "open":
            price = _num(trade.get("last_mark"))
            if price is None:
                price = _num(trade.get("entry_ask")) or 0.0
            moment = _when(trade.get("last_mark_at")) or _when(trade.get("entry_at")) or _ct()
            _close(trade, price, "Last mark", "flat (2:45 PM CT missed; last mark)", moment)
        _finalize(row, history_path)


def _loss_blocks(extra: Iterable[str] | None) -> set[str]:
    return {str(c).upper() for c in (extra or []) if c}


def run_shadow_cycle(
    *,
    rip_radar: dict[str, Any] | None = None,
    now: datetime | None = None,
    loss_cooldown_contracts: Iterable[str] | None = None,
    store_path: Path | None = None,
    history_path: Path | None = None,
    quote: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    pick: Callable[[str, float, set[str]], dict[str, Any] | None] | None = None,
    send: Callable[[str], dict[str, Any]] | None = None,
    telegram: bool | None = None,
    orders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record v2 trades for new RIP signals, mark open ones, flat at 2:45 PM CT.

    Called from the scan loop with the RIP board it just built, and from the
    fast loop with ``rip_radar=None`` (marks, flatten, and summary only).
    Never touches a live alert or paper order.
    """
    moment = _ct(now)
    today = moment.date()
    day = today.isoformat()
    quote_fn = quote or _default_quote
    pick_fn = pick or _default_pick
    with _LOCK:
        state = read_day(store_path)
        if state and str(state.get("day") or "") != day:
            _rollover(state, history_path)
            start = state.get("start_day")
            state = {"day": day, "signals": [], "start_day": start}
        if not state:
            history = read_history(history_path)
            state = {"day": day, "signals": [], "start_day": _start_day({}, history, day)}
        signals: list[dict[str, Any]] = list(state.get("signals") or [])
        seen = {str(row.get("key")) for row in signals}
        new_rows: list[dict[str, Any]] = []
        board = rip_radar if isinstance(rip_radar, dict) else {}
        tickets = board.get("buy_rip") or board.get("buy_now") or []
        blocked: set[str] | None = None
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            if str(ticket.get("action") or "BUY_RIP") != "BUY_RIP":
                continue
            key = signal_key(ticket)
            if not key or key in seen:
                continue
            seen.add(key)
            if blocked is None:
                blocked = _loss_blocks(loss_cooldown_contracts) | _losing_occs(read_history(history_path), state)
            symbol = str(ticket.get("symbol") or "").upper()
            signaled = _when(ticket.get("signaled_at")) or moment
            row: dict[str, Any] = {
                "key": key,
                "day": day,
                "symbol": symbol,
                "signaled_at": signaled.isoformat(),
                "signaled_ct": _hm(signaled),
                "seen_at": moment.isoformat(),
                "spot": _num(ticket.get("spot")),
                "live": {
                    "contract": str(ticket.get("contract") or "").upper() or None,
                    "strike": _num(ticket.get("strike")),
                    "expiry": str(ticket.get("expiry") or "")[:10] or None,
                    "dte": ticket.get("dte"),
                    "moneyness_pct": _num(ticket.get("moneyness_pct")),
                    "ask": _num(ticket.get("ask")),
                    "ask_label": money_label("Ask", ticket.get("ask")),
                },
            }
            why = None
            if symbol == "SPX":
                why = "SPX stays alert-only"
            elif moment.time() < OPEN_CT:
                why = "before the 8:30 AM CT open"
            elif moment.time() >= FLAT_CT:
                why = "at or after the 2:45 PM CT flat time (no overnight)"
            picked = None
            if why is None:
                try:
                    picked, reason = choose_v2_contract(
                        ticket, blocked=blocked, quote=quote_fn, pick=pick_fn, today=today
                    )
                except Exception as exc:  # noqa: BLE001
                    picked, reason = None, f"v2 pick failed: {str(exc)[:120]}"
                if picked is None:
                    why = reason
            if picked is not None and picked.get("contract"):
                holding = any(
                    (r.get("v2") or {}).get("status") == "open"
                    and (r.get("v2") or {}).get("contract") == picked["contract"]
                    for r in signals + new_rows
                )
                if holding:
                    why = f"v2 already holding {picked['contract']}"
                    picked = None
            if picked is None:
                row["v2"] = {"status": "no_trade", "reason": why or "no v2 trade"}
            else:
                row["v2"] = {
                    "status": "open",
                    "reason": reason,
                    "contract": picked.get("contract"),
                    "strike": picked.get("strike"),
                    "expiry": picked.get("expiry"),
                    "dte": picked.get("dte"),
                    "moneyness_pct": picked.get("moneyness_pct"),
                    "same_as_live": picked.get("same_as_live"),
                    "weekly_fallback": picked.get("weekly_fallback"),
                    "entry_ask": round(float(picked["ask"]), 4),
                    "entry_label": money_label("Ask", picked["ask"]),
                    "entry_quote_source": picked.get("quote_source"),
                    "entry_at": moment.isoformat(),
                    "entry_ct": _hm(moment),
                    "peak_pct": 0.0,
                }
            new_rows.append(row)
        signals.extend(new_rows)
        state["signals"] = signals
        closed = mark_open_trades(state, moment, quote=quote_fn)
        for row in signals:
            _finalize(row, history_path)
        sent_line = None
        if moment.time() >= FLAT_CT and not state.get("summary_sent"):
            open_left = any((r.get("v2") or {}).get("status") == "open" for r in signals)
            if signals and not open_left:
                want = telegram if telegram is not None else os.environ.get("RIP_V2_SHADOW_TELEGRAM", "1") != "0"
                state["summary_sent"] = True
                if want:
                    summary = build_stats(state, read_history(history_path), orders=orders, load_orders=orders is None)
                    sent_line = format_summary(summary, day=day)
                    try:
                        (send or _default_send)(sent_line)
                    except Exception as exc:  # noqa: BLE001
                        logger.info("rip v2 shadow telegram failed: %s", exc)
        state["updated_at"] = moment.isoformat()
        write_day(state, store_path)
    return {"ok": True, "new": len(new_rows), "closed_now": len(closed), "summary": sent_line}


def _default_send(text: str) -> dict[str, Any]:
    from odte_scanner.alerts.telegram import send_telegram_text

    return send_telegram_text(text)


# ---------------------------------------------------------------- stats


def side_stats(pcts: list[float], usd: list[float]) -> dict[str, Any]:
    n = len(pcts)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate_pct": None, "avg_pct": None, "pnl_usd": 0.0, "profit_factor": None}
    gains = sum(x for x in usd if x > 0)
    losses = -sum(x for x in usd if x < 0)
    wins = sum(1 for x in usd if x > 0)
    return {
        "n": n,
        "wins": wins,
        "win_rate_pct": round(wins / n * 100.0, 1),
        "avg_pct": round(sum(pcts) / n, 2),
        "pnl_usd": round(sum(usd), 2),
        "profit_factor": round(gains / losses, 2) if losses > 0 else None,
    }


def _current_trips(
    contracts: set[str],
    start_day: str,
    orders: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not contracts or not orders:
        return []
    from odte_scanner.trading.portal_book import round_trips

    trips = []
    for trip in round_trips(orders, include_other_desks=True):
        contract = str(trip.get("contract") or "").upper()
        if contract not in contracts:
            continue
        entered = _when(trip.get("entered_at") or trip.get("exited_at"))
        if entered is not None and entered.date().isoformat() < start_day:
            continue
        trips.append(trip)
    return trips


def build_stats(
    state: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    orders: list[dict[str, Any]] | None = None,
    load_orders: bool = True,
) -> dict[str, Any]:
    today_rows = list(state.get("signals") or [])
    logged_keys = {(r.get("day"), r.get("key")) for r in history}
    rows = list(history) + [r for r in today_rows if (r.get("day"), r.get("key")) not in logged_keys]
    start = _start_day(state, history, str(state.get("day") or _ct().date().isoformat()))
    v2_pcts: list[float] = []
    v2_usd: list[float] = []
    no_trade = 0
    open_n = 0
    live_contracts: set[str] = set()
    for row in rows:
        live = (row.get("live") or {}).get("contract")
        if live:
            live_contracts.add(str(live).upper())
        trade = row.get("v2") or {}
        status = trade.get("status")
        if status == "closed":
            v2_pcts.append(float(trade.get("pnl_pct") or 0.0))
            v2_usd.append(float(trade.get("pnl_usd") or 0.0))
        elif status == "open":
            open_n += 1
        elif status == "no_trade":
            no_trade += 1
    if orders is None and load_orders:
        try:
            from odte_scanner.hot_state import read_ledger_orders

            orders = read_ledger_orders()
        except Exception as exc:  # noqa: BLE001
            logger.info("rip v2 shadow ledger read failed: %s", exc)
            orders = []
    trips = _current_trips(live_contracts, start, orders)
    cur_pcts: list[float] = []
    cur_usd: list[float] = []
    cur_actual = 0.0
    for trip in trips:
        entry = _num(trip.get("entry")) or 0.0
        exit_px = _num(trip.get("exit")) or 0.0
        if entry <= 0:
            continue
        cur_pcts.append((exit_px / entry - 1.0) * 100.0)
        cur_usd.append((exit_px - entry) * 100.0)
        cur_actual += float(trip.get("pnl") or 0.0)
    current = side_stats(cur_pcts, cur_usd)
    current["pnl_usd_actual_qty"] = round(cur_actual, 2)
    v2 = side_stats(v2_pcts, v2_usd)
    v2.update(open=open_n, no_trade=no_trade)
    return {
        "start_day": start,
        "signals": len(rows),
        "current": current,
        "v2": v2,
    }


def _signal_view(row: dict[str, Any]) -> dict[str, Any]:
    trade = row.get("v2") or {}
    live = row.get("live") or {}
    return {
        "symbol": row.get("symbol"),
        "signaled_ct": row.get("signaled_ct"),
        "live_contract": live.get("contract"),
        "live_ask_label": live.get("ask_label"),
        "live_moneyness_pct": live.get("moneyness_pct"),
        "status": trade.get("status"),
        "reason": trade.get("reason"),
        "contract": trade.get("contract"),
        "moneyness_pct": trade.get("moneyness_pct"),
        "entry_label": trade.get("entry_label"),
        "entry_ct": trade.get("entry_ct"),
        "last_mark_label": trade.get("last_mark_label"),
        "gain_pct": trade.get("gain_pct"),
        "peak_pct": trade.get("peak_pct"),
        "exit_label": trade.get("exit_label"),
        "exit_ct": trade.get("exit_ct"),
        "exit_reason": trade.get("exit_reason"),
        "pnl_pct": trade.get("pnl_pct"),
        "pnl_usd": trade.get("pnl_usd"),
    }


def build_block(
    *,
    store_path: Path | None = None,
    history_path: Path | None = None,
    orders: list[dict[str, Any]] | None = None,
    load_orders: bool = True,
) -> dict[str, Any]:
    state = read_day(store_path)
    history = read_history(history_path)
    stats = build_stats(state, history, orders=orders, load_orders=load_orders)
    return {
        "ok": True,
        "label": LABEL,
        "shadow_only": True,
        "not_traded": True,
        "day": state.get("day"),
        "start_day": stats["start_day"],
        "rules": RULES,
        "stats": {"current": stats["current"], "v2": stats["v2"], "signals": stats["signals"]},
        "today": [_signal_view(r) for r in state.get("signals") or []],
        "summary_sent": bool(state.get("summary_sent")),
        "updated_at": state.get("updated_at"),
        "note": NOTE,
    }


def public_block() -> dict[str, Any]:
    """Snapshot block. A failure here never breaks the snapshot."""
    try:
        return build_block()
    except Exception as exc:  # noqa: BLE001
        logger.warning("rip v2 shadow block failed: %s", exc)
        return {"ok": False, "label": LABEL, "shadow_only": True, "today": [], "error": str(exc)[:200], "note": "RIP-v2 shadow unavailable."}


def _fmt_money(value: Any) -> str:
    number = _num(value) or 0.0
    return ("+$" if number >= 0 else "−$") + f"{abs(number):,.0f}"


def _fmt_side(side: dict[str, Any]) -> str:
    if not side.get("n"):
        return "n=0"
    pf = side.get("profit_factor")
    return (
        f"n={side['n']} win {side['win_rate_pct']:.0f}% avg {side['avg_pct']:+.1f}% "
        f"{_fmt_money(side['pnl_usd'])} PF {'—' if pf is None else f'{pf:.2f}'}"
    )


def format_summary(stats: dict[str, Any], *, day: str) -> str:
    """One Telegram line. Ends in '· shadow only'."""
    try:
        label = date.fromisoformat(day).strftime("%b %-d")
    except ValueError:
        label = day
    try:
        since = date.fromisoformat(str(stats.get("start_day"))).strftime("%b %-d")
    except ValueError:
        since = str(stats.get("start_day"))
    return (
        f"🧪 RIP-v2 shadow {label} · since {since} per 1 contract · "
        f"v2 {_fmt_side(stats['v2'])} | current {_fmt_side(stats['current'])} · shadow only"
    )


_RUNNING = threading.Event()


def run_shadow_cycle_async(**kwargs: Any) -> bool:
    """Run one cycle on a daemon thread so the scan never waits on quotes.

    Skips when a cycle is already running. Returns True when one started.
    """
    if _RUNNING.is_set():
        return False
    _RUNNING.set()
    board = kwargs.get("rip_radar")
    if isinstance(board, dict):
        tickets = [dict(t) for t in (board.get("buy_rip") or board.get("buy_now") or []) if isinstance(t, dict)]
        kwargs["rip_radar"] = {"buy_rip": tickets}
    if kwargs.get("loss_cooldown_contracts") is not None:
        kwargs["loss_cooldown_contracts"] = set(kwargs["loss_cooldown_contracts"])

    def _work() -> None:
        try:
            run_shadow_cycle(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rip v2 shadow cycle failed: %s", exc)
        finally:
            _RUNNING.clear()

    threading.Thread(target=_work, name="rip-v2-shadow", daemon=True).start()
    return True


def flat_window_due(now: datetime | None = None, store_path: Path | None = None) -> bool:
    """True from 2:45 to 4:00 PM CT on a day with work left (open trades or no summary yet)."""
    moment = _ct(now)
    if not (FLAT_CT <= moment.time() < dtime(16, 0)):
        return False
    state = read_day(store_path)
    if str(state.get("day") or "") != moment.date().isoformat():
        return False
    signals = state.get("signals") or []
    if any((r.get("v2") or {}).get("status") == "open" for r in signals):
        return True
    return bool(signals) and not state.get("summary_sent")
