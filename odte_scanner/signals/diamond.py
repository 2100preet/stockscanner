"""Diamond research book. A momentum call study, not a live alert.

The rules are an opening-range breakout that also holds VWAP, makes a higher
low, and only triggers while SPY itself is up. The option result is a model:
delta 0.50 and a premium scaled to the opening range. It is not a chain fill.
``diamond_routed`` stays false while that model's average return is negative,
so this module does not send Telegram and does not place a Webull order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.edge.metrics import mean_interval, wilson_interval
from odte_scanner.signals.exit_rules import DEFAULT_GOLD_EXIT

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

# Liquid index products and the mega caps already on the challenge watchlist,
# plus AVGO. SPX is the index, read from Yahoo as ^GSPC.
UNIVERSE: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "NVDA",
    "TSLA",
    "AVGO",
    "AMD",
    "GOOGL",
    "MU",
    "SPX",
)
YAHOO_SYMBOL = {"SPX": "^GSPC"}
# The index names are the market. They do not need to outrun SPY.
RS_EXEMPT = frozenset({"SPY", "SPX"})
INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "SPX"})
# First three signals of the day. Ties at the same bar go to the more liquid name.
DAILY_CAP = 3
PRIORITY: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "SPX",
    "NVDA",
    "TSLA",
    "AVGO",
    "IWM",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "AMD",
    "GOOGL",
    "MU",
)

_GOLD = DEFAULT_GOLD_EXIT


@dataclass(frozen=True)
class DiamondRules:
    """Entry window in America/Chicago, and the #144 same-day option exits.

    Index ETFs are the 0–2 DTE product and single names are the nearest
    weekly. Both are marked through the same session here, because this study
    has no overnight option quote. The +30% / −40% lines are the gold same-day
    exit (``arm_pct`` and ``same_day_stop_pct``). The live clock is 10:30 CT,
    not the gold 2:30 PM flatten. A trail, when enabled, uses
    ``trail_keep_fraction`` instead of banking the +30%.
    """

    or_start: time = time(8, 30)
    or_end: time = time(9, 0)
    entry_start: time = time(9, 0)
    entry_end: time = time(10, 30)
    min_spy_return: float = 0.0005
    max_extension: float = 0.0035
    min_or_bars: int = 4
    stop_pct: float = -abs(_GOLD.same_day_stop_pct)
    target_pct: float = _GOLD.arm_pct
    trail: bool = False
    trail_keep: float = _GOLD.trail_keep_fraction
    # Holding the same rules to 2:30 PM CT was negative on this sample.
    # The morning flat is the exit that stayed non-negative on the neighboring
    # clocks (10:00, 10:15, 10:30, 10:45), with both halves positive.
    flat_ct: time = time(10, 30)
    premium_floor: float = 0.006
    premium_or_frac: float = 0.40
    delta: float = 0.50

    def rules_id(self) -> str:
        return (
            f"or{self.or_start.strftime('%H%M')}-{self.or_end.strftime('%H%M')}"
            f"-until{self.entry_end.strftime('%H%M')}"
            f"-ext{int(round(self.max_extension * 10000))}"
            f"-spy{int(round(self.min_spy_return * 10000))}"
            f"-tgt{int(self.target_pct)}-stop{int(abs(self.stop_pct))}"
            f"-flat{self.flat_ct.strftime('%H%M')}"
            f"-trail{int(self.trail)}-keep{int(self.trail_keep * 100)}"
            f"-prem{int(self.premium_or_frac * 100)}"
        )


PRIMARY_RULES = DiamondRules()


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def ct(self) -> datetime:
        ts = self.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        return ts.astimezone(CT)


def _utc_key(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    return ts.astimezone(timezone.utc).replace(microsecond=0)


def rth_bars(bars: list[Bar]) -> list[Bar]:
    """Regular session only, 8:30–15:00 CT, in time order."""
    kept = [bar for bar in bars if time(8, 30) <= bar.ct().time() < time(15, 0)]
    kept.sort(key=lambda bar: bar.ct())
    return kept


def group_sessions(bars: list[Bar]) -> dict[date, list[Bar]]:
    out: dict[date, list[Bar]] = {}
    for bar in rth_bars(bars):
        out.setdefault(bar.ct().date(), []).append(bar)
    return out


def _vwap(bars: list[Bar]) -> float:
    num = 0.0
    den = 0.0
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = bar.volume if bar.volume > 0 else 0.0
        if vol <= 0:
            num += typical
            den += 1.0
        else:
            num += typical * vol
            den += vol
    if den <= 0:
        return bars[-1].close
    return num / den


def _premium(entry: float, or_high: float, or_low: float, rules: DiamondRules) -> float:
    width = max(0.0, or_high - or_low)
    return max(entry * rules.premium_floor, rules.premium_or_frac * width)


def index_market(spy_bars: list[Bar]) -> dict[datetime, tuple[float, float]]:
    """Map each SPY bar to (session open, that bar's close)."""
    out: dict[datetime, tuple[float, float]] = {}
    for _day, session in group_sessions(spy_bars).items():
        if not session:
            continue
        opened = session[0].open
        if opened <= 0:
            continue
        for bar in session:
            out[_utc_key(bar.ts)] = (opened, bar.close)
    return out


def first_signal(
    symbol: str,
    session: list[Bar],
    market: dict[datetime, tuple[float, float]],
    rules: DiamondRules | None = None,
) -> int | None:
    """Index of the first bar that passes, or None. One signal per session."""
    rules = rules or PRIMARY_RULES
    if len(session) < rules.min_or_bars + 1 or session[0].open <= 0:
        return None
    or_bars = [bar for bar in session if rules.or_start <= bar.ct().time() < rules.or_end]
    if len(or_bars) < rules.min_or_bars:
        return None
    or_high = max(bar.high for bar in or_bars)
    or_low = min(bar.low for bar in or_bars)
    if or_high <= 0:
        return None
    opened = session[0].open
    for i, bar in enumerate(session):
        clock = bar.ct().time()
        if clock < rules.entry_start:
            continue
        if clock >= rules.entry_end:
            break
        if i == 0:
            continue
        prev = session[i - 1]
        if bar.close <= or_high:
            continue
        extension = (bar.close - or_high) / or_high
        if extension > rules.max_extension:
            continue
        if bar.low <= prev.low or bar.close <= prev.close:
            continue
        if bar.close <= _vwap(session[: i + 1]):
            continue
        spot = market.get(_utc_key(bar.ts))
        if spot is None:
            continue
        spy_open, spy_close = spot
        if spy_open <= 0:
            continue
        spy_ret = spy_close / spy_open - 1.0
        if spy_ret <= rules.min_spy_return:
            continue
        if symbol not in RS_EXEMPT:
            sym_ret = bar.close / opened - 1.0
            if sym_ret < spy_ret:
                continue
        return i
    return None


def _option_pct(entry: float, exit_px: float, premium: float, delta: float) -> float:
    if premium <= 0:
        return 0.0
    return (delta * (exit_px - entry) / premium) * 100.0


def simulate(
    symbol: str,
    session: list[Bar],
    entry_index: int,
    rules: DiamondRules | None = None,
) -> dict[str, Any]:
    """Mark the call from the signal close to the #144-style same-day exit.

    On a bar that could have hit both the stop and the target, the stop wins.
    A stop or trail that gaps through fills at the open.
    """
    rules = rules or PRIMARY_RULES
    entry_bar = session[entry_index]
    entry = entry_bar.close
    or_bars = [bar for bar in session if rules.or_start <= bar.ct().time() < rules.or_end]
    or_high = max(bar.high for bar in or_bars)
    or_low = min(bar.low for bar in or_bars)
    premium = _premium(entry, or_high, or_low, rules)
    stop_move = premium * (rules.stop_pct / 100.0) / rules.delta
    target_move = premium * (rules.target_pct / 100.0) / rules.delta
    stop_px = entry + stop_move
    target_px = entry + target_move
    peak = entry
    armed = False
    exit_px = session[-1].close
    reason = "close"
    exit_at = session[-1].ct()

    for bar in session[entry_index + 1 :]:
        clock = bar.ct().time()
        if clock >= rules.flat_ct:
            exit_px = bar.open
            exit_at = bar.ct()
            reason = "flat"
            if exit_px <= stop_px:
                exit_px = min(exit_px, stop_px)
                reason = "stop"
            elif not rules.trail and exit_px >= target_px:
                reason = "target"
            elif rules.trail and armed:
                trail_px = entry + rules.trail_keep * (peak - entry)
                if exit_px <= trail_px:
                    reason = "trail"
                    exit_px = min(exit_px, trail_px)
            break
        if armed:
            trail_px = entry + rules.trail_keep * (peak - entry)
            if bar.low <= trail_px:
                exit_px = bar.open if bar.open <= trail_px else trail_px
                exit_at = bar.ct()
                reason = "trail"
                break
            if bar.high > peak:
                peak = bar.high
            continue
        if bar.low <= stop_px:
            exit_px = bar.open if bar.open < stop_px else stop_px
            exit_at = bar.ct()
            reason = "stop"
            break
        if bar.high >= target_px:
            if not rules.trail:
                exit_px = bar.open if bar.open > target_px else target_px
                exit_at = bar.ct()
                reason = "target"
                break
            peak = max(peak, bar.high)
            trail_px = entry + rules.trail_keep * (peak - entry)
            if bar.low <= trail_px:
                exit_px = bar.open if bar.open <= trail_px else trail_px
                exit_at = bar.ct()
                reason = "trail"
                break
            armed = True
            continue
        if bar.high > peak:
            peak = bar.high
    else:
        exit_at = session[-1].ct()

    opt = _option_pct(entry, exit_px, premium, rules.delta)
    und = (exit_px - entry) / entry * 100.0 if entry else 0.0
    return {
        "symbol": symbol,
        "at": entry_bar.ct().astimezone(timezone.utc).isoformat(),
        "at_ct": entry_bar.ct().strftime("%H:%M"),
        "day": entry_bar.ct().date().isoformat(),
        "entry": round(entry, 4),
        "exit": round(exit_px, 4),
        "option_pct": opt,
        "underlying_pct": und,
        "premium": premium,
        "reason": reason,
        "index_product": symbol in INDEX_ETFS,
    }


def run_symbol(
    symbol: str,
    bars: list[Bar],
    market: dict[datetime, tuple[float, float]],
    rules: DiamondRules | None = None,
) -> list[dict[str, Any]]:
    rules = rules or PRIMARY_RULES
    trades: list[dict[str, Any]] = []
    for session in group_sessions(bars).values():
        idx = first_signal(symbol, session, market, rules)
        if idx is None:
            continue
        trades.append(simulate(symbol, session, idx, rules))
    return trades


def run_universe(
    bars_by_symbol: dict[str, list[Bar]],
    rules: DiamondRules | None = None,
) -> list[dict[str, Any]]:
    rules = rules or PRIMARY_RULES
    spy = bars_by_symbol.get("SPY") or []
    market = index_market(spy)
    trades: list[dict[str, Any]] = []
    for symbol, bars in bars_by_symbol.items():
        trades.extend(run_symbol(symbol, bars, market, rules))
    trades.sort(key=lambda row: (row["at"], row["symbol"]))
    return trades


def apply_daily_cap(trades: list[dict[str, Any]], limit: int = DAILY_CAP) -> list[dict[str, Any]]:
    """The first ``limit`` signals each session, in the order they triggered.

    This is the book a live scan can actually send: it does not skip an early
    name because a later index might also qualify.
    """
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
        picked.extend(rows[: max(0, limit)])
    picked.sort(key=lambda row: (str(row.get("at") or ""), rank.get(str(row.get("symbol") or ""), 99)))
    return picked


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """n, win rate, average option percent, 95% interval, and the time split."""
    ordered = sorted(trades, key=lambda row: (row.get("at") or "", row.get("symbol") or ""))
    opts = [float(row["option_pct"]) for row in ordered]
    unds = [float(row["underlying_pct"]) for row in ordered]
    stats = mean_interval(opts)
    und = mean_interval(unds)
    n = int(stats["n"] or 0)
    wins = sum(1 for value in opts if value > 0)
    cut = n // 2
    h1 = mean_interval(opts[:cut]) if cut else {"n": 0, "mean": None}
    h2 = mean_interval(opts[cut:]) if n else {"n": 0, "mean": None}
    win_lo, win_hi = wilson_interval(wins, n)
    dollars = [float(row["option_pct"]) / 100.0 * float(row["premium"]) * 100.0 for row in ordered]
    dollar_stats = mean_interval(dollars)
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n) if n else None,
        "win_lo": win_lo,
        "win_hi": win_hi,
        "expectancy": stats["mean"],
        "lo": stats["lo"],
        "hi": stats["hi"],
        "h1_n": h1["n"],
        "h1": h1["mean"],
        "h2_n": h2["n"],
        "h2": h2["mean"],
        "underlying": und["mean"],
        "underlying_lo": und["lo"],
        "underlying_hi": und["hi"],
        "dollars": dollar_stats["mean"],
        "dollars_lo": dollar_stats["lo"],
        "dollars_hi": dollar_stats["hi"],
    }


def session_stats(bars: list[Bar]) -> dict[str, Any] | None:
    """Open, high, low, close, and the best call entry before the high."""
    session = rth_bars(bars)
    if not session:
        return None
    day = session[-1].ct().date()
    today = [bar for bar in session if bar.ct().date() == day]
    # Caller passes one session. Still guard a multi-day list by using the last day
    # only when every bar shares a day; otherwise use the whole list's last day.
    if any(bar.ct().date() != day for bar in session):
        today = [bar for bar in session if bar.ct().date() == day]
    else:
        today = session
    opened = today[0].open
    high = max(bar.high for bar in today)
    low = min(bar.low for bar in today)
    closed = today[-1].close
    high_at = next(bar for bar in today if bar.high == high)
    before = [bar for bar in today if bar.ct() <= high_at.ct()]
    entry = min(before, key=lambda bar: (bar.low, bar.ct()))
    return {
        "open": opened,
        "high": high,
        "low": low,
        "close": closed,
        "move_pct": (closed - opened) / opened * 100.0 if opened else None,
        "best_entry_ct": entry.ct().strftime("%H:%M"),
        "best_entry": entry.low,
        "high_ct": high_at.ct().strftime("%H:%M"),
        "low_to_high_pct": (high - entry.low) / entry.low * 100.0 if entry.low else None,
    }


def diamond_routed() -> bool:
    """Live Telegram and Webull stay off until a recorded study is non-negative.

    The recorded sample lives in ``odte_scanner.edge.diamond_study``. Importing
    it here would cycle through the edge report, so the switch is the study's
    own flag, checked by the tests. This function reads that flag lazily.
    """
    from odte_scanner.edge.diamond_study import ROUTED

    return bool(ROUTED)


RULES_TEXT = (
    "Calls only, one per symbol per day, on SPY QQQ IWM AAPL MSFT AMZN META "
    "NVDA TSLA AVGO AMD GOOGL MU and SPX. Opening range is 8:30–9:00 CT. "
    "Enter on the first 5-minute close from 9:00 CT until 10:30 CT that breaks "
    "that high, holds above VWAP, and makes a higher low and a higher close. "
    "SPY must be up more than 0.05% from its open. Other names must be up at "
    "least as much as SPY. Skip a break that is already more than 0.35% through "
    "the opening-range high. At most 3 symbols go out each day, the earliest "
    "ones, with SPY QQQ SPX NVDA TSLA AVGO winning a tie. The call is at the "
    "money or one strike in the money: 0–2 DTE on the index products, the "
    "nearest weekly on a single name. Sell the whole call at +30% premium, "
    "stop at −40%, and flat at 10:30 AM CT. A trail and a 2:30 PM flat were "
    "worse on this sample, so they are not the live exit. Option P&L is a "
    "0.50 delta model. Premium is the larger of 0.6% of spot and 40% of the "
    "opening-range width. No chain was available, so this is not a fill."
)
