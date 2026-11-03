"""Lunch-Break Credit. Bullish 0DTE put credit spread after the late-morning range.

SPY and QQQ can go to Webull paper. SPX is an alert with a modeled exit.
The structure is one spread: sell the at-the-money put and buy the put $2
lower, same expiry. Take profit when the spread can be bought back at half
the credit. Stop when the mark is twice the credit. Otherwise flat at 2:45 PM CT.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

import odte_scanner.edge.diamond_study  # noqa: F401  — finish the edge import cycle
from odte_scanner.signals.diamond import Bar, group_sessions
from odte_scanner.trading.ticks import normalize_option_limit

# Webull OpenAPI has accepted US multi-leg option orders since 2026-10-11.
# option_strategy VERTICAL is a two-strike spread on the same expiry. Lunch
# credit sends that combo. two_leg is only the fallback when the sandbox
# rejects the strategy. See odte_scanner.trading.webull.build_vertical_spread_order.
ORDER_PATH = "combo"

SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "SPX")
WEBULL_SYMBOLS = frozenset({"SPY", "QQQ"})
PRIORITY: tuple[str, ...] = ("SPY", "QQQ", "SPX")
DAILY_CAP = 2
WIDTH = 2.0
STRIKE_STEP = {"SPY": 1.0, "QQQ": 1.0, "SPX": 5.0}
CHASE_SLIPPAGE = 0.10
CHASE_MAX_AGE_SEC = 75
LOG_KIND = "LunchCredit"


@dataclass(frozen=True)
class LunchRules:
    """America/Chicago clocks. The range is the 5-minute highs from 10:00 to 12:00."""

    range_start: time = time(10, 0)
    range_end: time = time(12, 0)
    entry_start: time = time(12, 0)
    entry_end: time = time(14, 15)
    flat_ct: time = time(14, 45)
    width: float = WIDTH
    take_profit: float = 0.5
    stop_multiple: float = 2.0
    contracts: int = 1
    daily_cap: int = DAILY_CAP
    chase_slippage: float = CHASE_SLIPPAGE
    chase_seconds: int = CHASE_MAX_AGE_SEC

    def rules_id(self) -> str:
        return (
            f"range{self.range_start.strftime('%H%M')}-{self.range_end.strftime('%H%M')}"
            f"-break{self.entry_start.strftime('%H%M')}-{self.entry_end.strftime('%H%M')}"
            f"-w{self.width:g}-tp{self.take_profit:g}-stop{self.stop_multiple:g}"
            f"-flat{self.flat_ct.strftime('%H%M')}-cap{self.daily_cap}"
        )


PRIMARY_RULES = LunchRules()

RULES_TEXT = (
    "Bullish put credit spread on SPY, QQQ, and SPX. One name once a day, "
    "and at most two names. From 10:00 to 12:00 CT the desk tracks the "
    "5-minute range high. After 12:00 CT the first 5-minute close above that "
    "high is the entry, and no new entry after 2:15 PM CT. Sell the at-the-money "
    "0DTE put and buy the put $2 lower, one spread. The limit starts at the "
    "mid credit and walks toward the natural credit for 75 seconds, not through "
    "a 10% cap, then cancels. Take profit when the spread can be bought back at "
    "50% of the credit. Stop when the mark is twice the credit. Flat at 2:45 PM CT. "
    "No second entry. SPX is an alert, not a Webull order."
)


def _nearest(spot: float, step: float) -> float:
    if spot <= 0 or step <= 0:
        return 0.0
    return round(math.floor(spot / step + 0.5) * step, 2)


def atm_put(spot: float, symbol: str) -> float:
    """Nearest listed strike for the short put. Half a step rounds up."""
    return _nearest(spot, float(STRIKE_STEP.get(symbol, 1.0)))


def long_put(short_strike: float, width: float = WIDTH) -> float:
    """The long put is exactly ``width`` dollars below the short strike."""
    if short_strike <= 0:
        return 0.0
    return round(float(short_strike) - float(width), 2)


def range_high(session: list[Bar], rules: LunchRules | None = None) -> float | None:
    """Highest 5-minute high with a stamp in [10:00, 12:00) CT. None if that window is empty."""
    rules = rules or PRIMARY_RULES
    highs = [
        bar.high
        for bar in session
        if rules.range_start <= bar.ct().time() < rules.range_end and bar.high > 0
    ]
    if not highs:
        return None
    return max(highs)


def first_break(session: list[Bar], rules: LunchRules | None = None) -> int | None:
    """Index of the first 5-minute close above the range high, inside 12:00–14:15 CT.

    One signal per session. A later close, including one after a stop, is not
    a second entry. A close that only ties the high does not trigger.
    """
    rules = rules or PRIMARY_RULES
    high = range_high(session, rules)
    if high is None:
        return None
    for i, bar in enumerate(session):
        clock = bar.ct().time()
        if clock < rules.entry_start or clock >= rules.entry_end:
            continue
        if bar.close > high + 1e-9:
            return i
    return None


def _pos(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def leg_mid(bid: float | None, ask: float | None) -> float | None:
    bid_f = _pos(bid)
    ask_f = _pos(ask)
    if bid_f is None or ask_f is None:
        return None
    if ask_f + 1e-9 < bid_f:
        return None
    return (bid_f + ask_f) / 2.0


def spread_mid(short_bid: float | None, short_ask: float | None, long_bid: float | None, long_ask: float | None) -> float | None:
    """Mark of the spread: short mid minus long mid. This is the credit mark."""
    short = leg_mid(short_bid, short_ask)
    long = leg_mid(long_bid, long_ask)
    if short is None or long is None:
        return None
    return short - long


def natural_credit(short_bid: float | None, long_ask: float | None) -> float | None:
    """Credit the market will pay now: sell the short put at the bid, buy the long put at the ask."""
    bid_f = _pos(short_bid)
    ask_f = _pos(long_ask)
    if bid_f is None or ask_f is None:
        return None
    return bid_f - ask_f


def natural_debit(short_ask: float | None, long_bid: float | None) -> float | None:
    """Debit to buy the spread back: buy the short put at the ask, sell the long put at the bid."""
    ask_f = _pos(short_ask)
    bid_f = _pos(long_bid)
    if ask_f is None or bid_f is None:
        return None
    return ask_f - bid_f


def max_risk_usd(credit: float, *, width: float = WIDTH, contracts: int = 1) -> float:
    """(width − credit) × 100 × contracts. A credit above the width has no remaining risk."""
    try:
        credit_f = float(credit)
    except (TypeError, ValueError):
        return 0.0
    risk = max(0.0, float(width) - credit_f) * 100.0 * max(1, int(contracts))
    return round(risk, 2)


def spread_pnl(credit: float, debit: float, contracts: int = 1) -> tuple[float, float]:
    """Dollars and percent of the credit. Percent is (credit − debit) / credit."""
    qty = max(1, int(contracts))
    dollars = (float(credit) - float(debit)) * 100.0 * qty
    pct = 0.0 if float(credit) <= 0 else (float(credit) - float(debit)) / float(credit) * 100.0
    return round(dollars, 2), round(pct, 4)


def take_profit_debit(credit: float, rules: LunchRules | None = None) -> float:
    rules = rules or PRIMARY_RULES
    return float(credit) * float(rules.take_profit)


def stop_mark(credit: float, rules: LunchRules | None = None) -> float:
    rules = rules or PRIMARY_RULES
    return float(credit) * float(rules.stop_multiple)


def exit_reason(credit: float, mark: float, *, flat: bool, rules: LunchRules | None = None) -> str | None:
    """``target``, ``stop``, ``flat``, or None while the spread is still open.

    A flat clock wins so the 2:45 bar is not also scored as a stop. Otherwise
    a mark at or above twice the credit is the stop, and a mark at or below
    half the credit is the take profit.
    """
    rules = rules or PRIMARY_RULES
    if credit <= 0:
        return "flat" if flat else None
    if flat:
        return "flat"
    if mark + 1e-9 >= stop_mark(credit, rules):
        return "stop"
    if mark <= take_profit_debit(credit, rules) + 1e-9:
        return "target"
    return None


def walk_marks(
    credit: float,
    marks: list[tuple[datetime, float, float, float]],
    rules: LunchRules | None = None,
) -> dict[str, Any]:
    """Walk spread marks after the fill.

    Each mark is (time, open, high, low) of the spread's value. High is the
    expensive print and low is the cheap one. A bar that can both stop and
    pay the target takes the stop. The 2:45 CT bar exits at its open.
    """
    rules = rules or PRIMARY_RULES
    exit_px = credit
    exit_at = marks[-1][0] if marks else None
    reason = "close"
    target = take_profit_debit(credit, rules)
    stop_line = stop_mark(credit, rules)
    for moment, opened, high, low in marks:
        if moment.time() >= rules.flat_ct:
            exit_px = opened
            exit_at = moment
            reason = "flat"
            break
        if high + 1e-9 >= stop_line:
            exit_px = opened if opened >= stop_line else stop_line
            exit_at = moment
            reason = "stop"
            break
        if low <= target + 1e-9:
            exit_px = opened if opened <= target else target
            exit_at = moment
            reason = "target"
            break
        exit_px = opened
        exit_at = moment
    open_trade = reason == "close"
    dollars, pct = (None, None) if open_trade else spread_pnl(credit, exit_px, rules.contracts)
    return {
        "credit": credit,
        "exit_debit": None if open_trade else exit_px,
        "pnl_usd": dollars,
        "option_pct": pct,
        "reason": reason,
        "exit_at": exit_at,
    }


def opening_limit(mid: float | None, natural: float | None) -> float | None:
    """First credit limit is the mid, snapped down onto the option tick, and never below the natural."""
    mid_f = _pos(mid)
    if mid_f is None:
        return None
    snapped = normalize_option_limit(mid_f, side="SELL")
    nat = natural if natural is not None else None
    if nat is not None and snapped + 1e-9 < nat:
        snapped = normalize_option_limit(nat, side="SELL")
    if snapped <= 0:
        return None
    return snapped


def credit_floor(baseline: float, rules: LunchRules | None = None) -> float:
    """Lowest credit the chase will post. Ten percent under the original mid."""
    rules = rules or PRIMARY_RULES
    raw = float(baseline) * (1.0 - float(rules.chase_slippage))
    if raw <= 0:
        return 0.0
    # A sell snaps down. Walk the floor up onto a legal tick so the cap is not pierced.
    floored = normalize_option_limit(raw, side="SELL")
    if floored + 1e-9 < raw:
        floored = normalize_option_limit(raw, side="BUY")
    return floored


def next_credit_limit(
    baseline: float | None,
    natural: float | None,
    working: float | None,
    rules: LunchRules | None = None,
) -> float | None:
    """Next credit, closer to the natural bid, and not through the slippage cap.

    Returns None when the working limit is already at that target.
    """
    base = _pos(baseline)
    nat = natural if natural is not None else None
    if base is None or nat is None:
        return None
    floor = credit_floor(base, rules)
    target_raw = max(float(nat), floor)
    target = normalize_option_limit(target_raw, side="SELL")
    if target + 1e-9 < floor:
        target = normalize_option_limit(floor, side="BUY")
    if target <= 0:
        return None
    if working is not None and target + 0.001 >= float(working):
        return None
    return target


def credit_chase_action(
    *,
    limit: float | None,
    natural: float | None,
    baseline: float | None,
    age_sec: float | None,
    past_window: bool,
    rules: LunchRules | None = None,
) -> str:
    """``cancel``, ``cap``, ``reprice``, or ``hold``."""
    rules = rules or PRIMARY_RULES
    if past_window or (age_sec is not None and age_sec >= rules.chase_seconds):
        return "cancel"
    if next_credit_limit(baseline, natural, limit, rules) is not None:
        return "reprice"
    base = _pos(baseline)
    nat = natural if natural is not None else None
    if base is not None and nat is not None and float(nat) + 1e-9 < credit_floor(base, rules):
        return "cap"
    return "hold"


def run_symbol(symbol: str, bars: list[Bar], rules: LunchRules | None = None) -> list[dict[str, Any]]:
    """One break per session. The trade row carries the spot and the two strikes."""
    rules = rules or PRIMARY_RULES
    trades: list[dict[str, Any]] = []
    for day, session in group_sessions(bars).items():
        found = first_break(session, rules)
        if found is None:
            continue
        bar = session[found]
        high = range_high(session, rules) or 0.0
        short = atm_put(bar.close, symbol)
        long = long_put(short, rules.width)
        trades.append(
            {
                "symbol": symbol,
                "at": bar.ct().isoformat(),
                "at_ct": bar.ct().strftime("%H:%M"),
                "day": day.isoformat(),
                "entry": round(bar.close, 4),
                "range_high": round(high, 4),
                "short_strike": short,
                "long_strike": long,
                "width": rules.width,
                "contracts": rules.contracts,
                "webull": symbol in WEBULL_SYMBOLS,
            }
        )
    return trades


def apply_daily_cap(trades: list[dict[str, Any]], limit: int = DAILY_CAP) -> list[dict[str, Any]]:
    """First ``limit`` breaks each day. A tie goes to SPY, then QQQ, then SPX."""
    rank = {symbol: i for i, symbol in enumerate(PRIORITY)}
    by_day: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        by_day.setdefault(str(trade.get("day") or ""), []).append(trade)
    picked: list[dict[str, Any]] = []
    for day in sorted(by_day):
        rows = sorted(
            by_day[day],
            key=lambda row: (str(row.get("at") or ""), rank.get(str(row.get("symbol") or ""), 99)),
        )
        seen: set[str] = set()
        kept: list[dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol in seen:
                continue
            seen.add(symbol)
            kept.append(row)
            if len(kept) >= limit:
                break
        picked.extend(kept)
    picked.sort(key=lambda row: (str(row.get("at") or ""), rank.get(str(row.get("symbol") or ""), 99)))
    return picked


def replay(bars_by_symbol: dict[str, list[Bar]], rules: LunchRules | None = None) -> list[dict[str, Any]]:
    rules = rules or PRIMARY_RULES
    raw: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        bars = bars_by_symbol.get(symbol) or []
        if bars:
            raw.extend(run_symbol(symbol, bars, rules))
    return apply_daily_cap(raw, rules.daily_cap)
