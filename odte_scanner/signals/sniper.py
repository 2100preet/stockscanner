"""Sniper research book. A 15-minute opening-range retest, calls and puts.

The option result is the same kind of model Diamond uses: delta 0.50 and a
premium scaled to the opening range. It is not a chain fill. ``sniper_routed``
stays false unless the recorded sample's average is non-negative, the 95%
interval is above zero, and both halves are positive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

# Importing Diamond directly, before the edge package has finished loading,
# hits a cycle through the edge panel. The study module is the path that
# already completes that cycle.
import odte_scanner.edge.diamond_study  # noqa: F401
from odte_scanner.signals.diamond import (
    DAILY_CAP,
    INDEX_ETFS,
    PRIORITY,
    UNIVERSE,
    Bar,
    _vwap,
    apply_daily_cap,
    group_sessions,
    summarize,
)

CT_FLAT = time(10, 30)

# Re-export the shared universe. SPX is alerted and is not a Webull order.
__all__ = [
    "DAILY_CAP",
    "INDEX_ETFS",
    "PRIORITY",
    "UNIVERSE",
    "PRIMARY_RULES",
    "Bar",
    "apply_daily_cap",
    "first_signal",
    "run_universe",
    "simulate",
    "summarize",
    "sniper_routed",
]


@dataclass(frozen=True)
class SniperRules:
    """America/Chicago clocks. The live book flats at 10:30 CT.

    A wick back inside the opening range was tested as a stop and rejected:
    it fired on most retests and the interval fell through zero. The hard
    stop is −40% of premium. The live exit sells the whole position at +25%.
    A half trim plus a trail was the earlier book; the full exit cleared a
    higher average on the same entries, so that is the one that ships.
    """

    or_start: time = time(8, 30)
    or_end: time = time(8, 45)
    entry_end: time = time(10, 30)
    flat_ct: time = CT_FLAT
    min_or_bars: int = 3
    max_extension: float = 0.0035
    trim_pct: float = 25.0
    stop_pct: float = -40.0
    trail: bool = False
    full_exit: bool = True
    or_stop: bool = False
    need_ema: bool = True
    premium_floor: float = 0.006
    premium_or_frac: float = 0.40
    delta: float = 0.50

    def rules_id(self) -> str:
        return (
            f"or{self.or_start.strftime('%H%M')}-{self.or_end.strftime('%H%M')}"
            f"-retest-ema{int(self.need_ema)}-until{self.entry_end.strftime('%H%M')}"
            f"-trim{int(self.trim_pct)}-stop{int(abs(self.stop_pct))}"
            f"-flat{self.flat_ct.strftime('%H%M')}-trail{int(self.trail)}"
            f"-full{int(self.full_exit)}-orstop{int(self.or_stop)}-cap{DAILY_CAP}"
        )


PRIMARY_RULES = SniperRules()

RULES_TEXT = (
    "Calls and puts, one per symbol per day, on SPY QQQ IWM AAPL MSFT AMZN "
    "META NVDA TSLA AVGO AMD GOOGL MU and SPX. The opening range is the first "
    "15 minutes, 8:30–8:45 CT. Enter on the first later 5-minute bar, through "
    "10:30 CT, that retests the broken side and closes back outside it while "
    "the 9 and 21 EMAs on 5-minute bars agree. Skip a retest already more than "
    "0.35% through the level. At most 3 symbols go out each day, the earliest "
    "ones, with SPY QQQ SPX NVDA TSLA AVGO winning a tie. The contract is at "
    "the money or one strike in the money: 0–2 DTE on the index products, the "
    "nearest weekly on a single name. Sell the whole position at +25% premium. "
    "Hard stop −40%. Flat at 10:30 AM CT. Selling half at +25% and trailing "
    "the rest was tested on the same entries and made less. A stop that treats "
    "any tick back inside the opening range as an exit was tested and dropped: "
    "it stopped out the retest itself. Puts are in the book; calls alone did "
    "not clear the interval. Option P&L is a 0.50 delta model. Premium is the "
    "larger of 0.6% of spot and 40% of the opening-range width. No chain was "
    "available, so this is not a fill."
)


def _ema(values: list[float], length: int) -> list[float]:
    k = 2.0 / (length + 1.0)
    out: list[float] = []
    acc: float | None = None
    for value in values:
        acc = value if acc is None else value * k + acc * (1.0 - k)
        out.append(acc)
    return out


def _premium(entry: float, or_high: float, or_low: float, rules: SniperRules) -> float:
    width = max(0.0, or_high - or_low)
    return max(entry * rules.premium_floor, rules.premium_or_frac * width)


def _or_bounds(session: list[Bar], rules: SniperRules) -> tuple[float, float] | None:
    bars = [bar for bar in session if rules.or_start <= bar.ct().time() < rules.or_end]
    if len(bars) < rules.min_or_bars:
        return None
    return max(bar.high for bar in bars), min(bar.low for bar in bars)


def _stack_ok(side: int, close: float, ema9: float, ema21: float) -> bool:
    if side > 0:
        return close > ema9 > ema21
    return close < ema9 < ema21


def first_signal(
    session: list[Bar],
    ema9: list[float],
    ema21: list[float],
    offset: int,
    rules: SniperRules | None = None,
) -> tuple[int, int] | None:
    """First (bar index, side) after the 15-minute range. Side is +1 or −1.

    ``ema9`` and ``ema21`` are the continuous series. ``offset`` is the index
    of this session's first bar in that series.
    """
    rules = rules or PRIMARY_RULES
    bounds = _or_bounds(session, rules)
    if bounds is None:
        return None
    or_high, or_low = bounds
    if or_high <= 0 or or_low <= 0:
        return None
    broke = {1: False, -1: False}
    for i, bar in enumerate(session):
        clock = bar.ct().time()
        if clock < rules.or_end:
            continue
        if clock >= rules.entry_end:
            break
        close = bar.close
        at = offset + i
        if at >= len(ema9) or at >= len(ema21):
            continue
        for side in (1, -1):
            level = or_high if side > 0 else or_low
            beyond = (side > 0 and close > or_high) or (side < 0 and close < or_low)
            if not broke[side]:
                if beyond:
                    broke[side] = True
                continue
            if not beyond:
                broke[side] = False
                continue
            tagged = (side > 0 and bar.low <= level * 1.0008) or (
                side < 0 and bar.high >= level * 0.9992
            )
            extension = abs(close - level) / level
            if not tagged or extension > rules.max_extension:
                continue
            if rules.need_ema and not _stack_ok(side, close, ema9[at], ema21[at]):
                continue
            return i, side
    return None


def simulate(
    symbol: str,
    session: list[Bar],
    entry_index: int,
    side: int,
    rules: SniperRules | None = None,
) -> dict[str, Any]:
    """Mark the option from the signal close.

    The live rule sells the whole position at the target. The older half-trim
    path remains for a rules object that turns ``full_exit`` off. A bar that
    can hit the stop and the target takes the stop. A gap fills at the open.
    """
    rules = rules or PRIMARY_RULES
    entry_bar = session[entry_index]
    entry = entry_bar.close
    bounds = _or_bounds(session, rules)
    if bounds is None:
        raise ValueError("session has no opening range")
    or_high, or_low = bounds
    prem = _premium(entry, or_high, or_low, rules)
    delta = rules.delta

    def dist(pct: float) -> float:
        return prem * (pct / 100.0) / delta

    stop_dist = dist(abs(rules.stop_pct))
    trim_dist = dist(rules.trim_pct)
    level = or_high if side > 0 else or_low

    def opt(px: float) -> float:
        if prem <= 0:
            return 0.0
        return (delta * side * (px - entry) / prem) * 100.0

    trimmed = False
    peak = entry
    exit_px = session[-1].close
    reason = "close"

    for bar in session[entry_index + 1 :]:
        clock = bar.ct().time()
        if clock >= rules.flat_ct:
            exit_px = bar.open
            reason = "flat"
            break
        if not trimmed:
            stopped = (side > 0 and bar.low <= entry - stop_dist) or (
                side < 0 and bar.high >= entry + stop_dist
            )
            lost_or = rules.or_stop and (
                (side > 0 and bar.low <= level) or (side < 0 and bar.high >= level)
            )
            hit = (side > 0 and bar.high >= entry + trim_dist) or (
                side < 0 and bar.low <= entry - trim_dist
            )
            if stopped or lost_or:
                if stopped:
                    raw = entry - side * stop_dist
                    if (side > 0 and bar.open < raw) or (side < 0 and bar.open > raw):
                        raw = bar.open
                    exit_px = raw
                    reason = "stop"
                else:
                    exit_px = (
                        bar.open
                        if ((side > 0 and bar.open < level) or (side < 0 and bar.open > level))
                        else level
                    )
                    reason = "or"
                break
            if hit and rules.full_exit:
                raw = entry + side * trim_dist
                if (side > 0 and bar.open > raw) or (side < 0 and bar.open < raw):
                    raw = bar.open
                exit_px = raw
                reason = "target"
                break
            if hit:
                trimmed = True
                peak = bar.high if side > 0 else bar.low
                if (side > 0 and bar.low <= entry) or (side < 0 and bar.high >= entry):
                    exit_px = entry
                    reason = "be"
                    break
                continue
        else:
            fav = side * (peak - entry)
            trail_px = entry + side * (0.5 * fav) if rules.trail and fav > 0 else entry
            if rules.or_stop and (
                (side > 0 and bar.low <= level) or (side < 0 and bar.high >= level)
            ):
                exit_px = level
                reason = "or"
                break
            hit_stop = (side > 0 and bar.low <= trail_px) or (side < 0 and bar.high >= trail_px)
            if hit_stop:
                exit_px = (
                    bar.open
                    if ((side > 0 and bar.open < trail_px) or (side < 0 and bar.open > trail_px))
                    else trail_px
                )
                reason = "trail" if rules.trail and fav > 0 and abs(trail_px - entry) > 1e-9 else "be"
                break
            if side > 0 and bar.high > peak:
                peak = bar.high
            if side < 0 and bar.low < peak:
                peak = bar.low

    full = opt(exit_px)
    if rules.full_exit:
        pnl = full
    else:
        pnl = 0.5 * rules.trim_pct + 0.5 * full if trimmed and reason != "stop" else full
    und = side * (exit_px - entry) / entry * 100.0 if entry else 0.0
    return {
        "symbol": symbol,
        "at": entry_bar.ct().astimezone(timezone.utc).isoformat(),
        "at_ct": entry_bar.ct().strftime("%H:%M"),
        "day": entry_bar.ct().date().isoformat(),
        "side": "C" if side > 0 else "P",
        "entry": round(entry, 4),
        "exit": round(exit_px, 4),
        "option_pct": pnl,
        "underlying_pct": und,
        "premium": prem,
        "reason": reason,
        "index_product": symbol in INDEX_ETFS,
    }


def _continuous(sessions: dict[date, list[Bar]]) -> tuple[dict[tuple[date, int], int], list[float], list[float]]:
    closes: list[float] = []
    index: dict[tuple[date, int], int] = {}
    for day in sorted(sessions):
        for i, bar in enumerate(sessions[day]):
            index[(day, i)] = len(closes)
            closes.append(bar.close)
    return index, _ema(closes, 9), _ema(closes, 21)


def run_symbol(
    symbol: str,
    bars: list[Bar],
    rules: SniperRules | None = None,
) -> list[dict[str, Any]]:
    rules = rules or PRIMARY_RULES
    sessions = group_sessions(bars)
    index, ema9, ema21 = _continuous(sessions)
    trades: list[dict[str, Any]] = []
    for day in sorted(sessions):
        session = sessions[day]
        offset = index.get((day, 0), 0)
        found = first_signal(session, ema9, ema21, offset, rules)
        if found is None:
            continue
        idx, side = found
        trades.append(simulate(symbol, session, idx, side, rules))
    return trades


def run_universe(
    bars_by_symbol: dict[str, list[Bar]],
    rules: SniperRules | None = None,
) -> list[dict[str, Any]]:
    rules = rules or PRIMARY_RULES
    trades: list[dict[str, Any]] = []
    for symbol, bars in bars_by_symbol.items():
        trades.extend(run_symbol(symbol, bars, rules))
    trades.sort(key=lambda row: (row["at"], row["symbol"]))
    return trades


def sniper_routed() -> bool:
    """Live Telegram and Webull stay off unless the recorded study clears."""
    from odte_scanner.edge.sniper_study import ROUTED

    return bool(ROUTED)
