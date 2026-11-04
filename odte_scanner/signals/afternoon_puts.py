"""Afternoon Index Puts. A same-session 5-minute put after the high is in.

SPY and QQQ can go to Webull paper. SPX is an alert with a modeled exit.
The option result in the Nov 2 replay uses Yahoo 5-minute put prints. Other
sessions without a put series use a Black-Scholes stand-in. Neither one is a
live fill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

import odte_scanner.edge.diamond_study  # noqa: F401  — finish the edge import cycle
from odte_scanner.signals.diamond import Bar, group_sessions

# Percent, not a fraction. Price must be at least this far under the session high.
HOD_DROP_PCT = 0.5

SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "SPX")
# Webull paper names. SPX is never an order.
WEBULL_SYMBOLS = frozenset({"SPY", "QQQ"})
PRIORITY: tuple[str, ...] = ("SPY", "QQQ", "SPX")
DAILY_CAP = 2
STRIKE_STEP = {"SPY": 1.0, "QQQ": 1.0, "SPX": 5.0}
MODEL_IV = {"SPY": 0.19, "QQQ": 0.26, "SPX": 0.16}
SESSION_FLAT = time(15, 0)


@dataclass(frozen=True)
class AfternoonRules:
    """America/Chicago clocks. Thresholds live here so a study can name them.

    The 9 and 21 EMAs are computed on the current session only. Carrying the
    prior day in moved the Nov 2 QQQ trigger off the 10:25 bar. A support
    level is a one-bar swing low: lower than the bar before it and at or
    below the bar after it. The entry is the first later 5-minute close that
    tags that broken low from below and finishes back underneath it.
    """

    entry_start: time = time(10, 0)
    entry_end: time = time(14, 15)
    flat_ct: time = time(14, 45)
    hod_drop_pct: float = HOD_DROP_PCT
    swing_left: int = 1
    swing_right: int = 1
    ema_fast: int = 9
    ema_slow: int = 21
    target_pct: float = 100.0
    stop_pct: float = 40.0
    trail_keep: float = 0.5
    contracts: int = 1
    daily_cap: int = DAILY_CAP

    def rules_id(self) -> str:
        return (
            f"hod{self.hod_drop_pct:g}-swing{self.swing_left}{self.swing_right}"
            f"-ema{self.ema_fast}{self.ema_slow}"
            f"-{self.entry_start.strftime('%H%M')}-{self.entry_end.strftime('%H%M')}"
            f"-half{int(self.target_pct)}-trail{self.trail_keep:g}"
            f"-stop{int(self.stop_pct)}-flat{self.flat_ct.strftime('%H%M')}"
            f"-cap{self.daily_cap}"
        )


PRIMARY_RULES = AfternoonRules()

RULES_TEXT = (
    "Puts only, on SPY, QQQ, and SPX. One name once a day, and at most two "
    "names. The session high has to be on an earlier 5-minute bar, with a "
    "lower high after it. Price is under the session VWAP, the 5-minute 9 EMA "
    "is under the 21 EMA, and the close is at least 0.5% under the session "
    "high. Enter on the first 5-minute close, from 10:00 to 14:15 CT, that "
    "tags a broken 5-minute swing low from below and closes back under it. "
    "The contract is the 0DTE put one strike out of the money, one contract. "
    "Sell half at +100% and trail the rest, out if it gives back half of the "
    "peak gain. Hard stop −40% from the fill. Flat at 2:45 PM CT. No second "
    "entry after a stop. SPY and QQQ are entered only when both have met "
    "these gates the same day. The first name waits until 2:15 PM CT. When "
    "the second confirms, both are entered on that bar at its prices. SPX "
    "is an alert and is not part of that check. SPX is not a Webull order."
)


def _ema(values: list[float], length: int) -> list[float]:
    k = 2.0 / (length + 1.0)
    out: list[float] = []
    acc: float | None = None
    for value in values:
        acc = value if acc is None else value * k + acc * (1.0 - k)
        out.append(acc)
    return out


def _vwap_at(session: list[Bar]) -> list[float]:
    num = 0.0
    den = 0.0
    out: list[float] = []
    for bar in session:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = bar.volume if bar.volume > 0 else 1.0
        num += typical * vol
        den += vol
        out.append(num / den if den else bar.close)
    return out


def _swings(session: list[Bar], left: int, right: int, *, kind: str) -> list[int]:
    found: list[int] = []
    last = len(session) - right
    for i in range(left, last):
        if kind == "low":
            value = session[i].low
            prior = all(value < session[i - k].low - 1e-9 for k in range(1, left + 1))
            later = all(value <= session[i + k].low + 1e-9 for k in range(1, right + 1))
        else:
            value = session[i].high
            prior = all(value > session[i - k].high + 1e-9 for k in range(1, left + 1))
            later = all(value >= session[i + k].high - 1e-9 for k in range(1, right + 1))
        if prior and later:
            found.append(i)
    return found


def one_strike_otm_put(spot: float, symbol: str) -> float:
    """Nearest strike, then one step further out of the money for a put."""
    step = float(STRIKE_STEP.get(symbol, 1.0))
    if spot <= 0 or step <= 0:
        return 0.0
    atm = round(spot / step) * step
    strike = round(atm - step, 2)
    if strike >= spot - 1e-9:
        strike = round(strike - step, 2)
    return strike


def _minutes_left(moment: datetime, rules: AfternoonRules) -> float:
    clock = moment.time()
    close_min = SESSION_FLAT.hour * 60 + SESSION_FLAT.minute
    now_min = clock.hour * 60 + clock.minute
    return float(max(1, close_min - now_min))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def model_put(spot: float, strike: float, minutes_left: float, iv: float) -> float:
    """Black-Scholes put. Used when a Yahoo 5-minute put series is absent."""
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = max(0.0, strike - spot)
    if minutes_left <= 0 or iv <= 0:
        return intrinsic
    t = max(minutes_left, 0.5) / (252.0 * 390.0)
    vol = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / vol
    d2 = d1 - vol
    price = strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(0.01, price)


def _clock_key(bar: Bar) -> str:
    return bar.ct().strftime("%H:%M")


def _align_option(session: list[Bar], option: list[Bar] | None) -> dict[str, Bar]:
    if not option:
        return {}
    return {_clock_key(bar): bar for bar in option}


def first_signal(session: list[Bar], rules: AfternoonRules | None = None) -> int | None:
    """Index of the first failed retest inside the entry window, or None.

    One signal per session. A later setup, including one after a stop, is not
    a second entry.
    """
    rules = rules or PRIMARY_RULES
    if len(session) < rules.swing_left + rules.swing_right + 2:
        return None
    ema_fast = _ema([bar.close for bar in session], rules.ema_fast)
    ema_slow = _ema([bar.close for bar in session], rules.ema_slow)
    vwap = _vwap_at(session)
    lows = _swings(session, rules.swing_left, rules.swing_right, kind="low")
    highs = _swings(session, rules.swing_left, rules.swing_right, kind="high")
    broken: dict[int, int] = {}
    for pivot in lows:
        level = session[pivot].low
        for j in range(pivot + rules.swing_right + 1, len(session)):
            if session[j].close < level:
                broken[pivot] = j
                break
    for i, bar in enumerate(session):
        clock = bar.ct().time()
        if clock < rules.entry_start or clock >= rules.entry_end:
            continue
        hod_i = max(range(i + 1), key=lambda j: session[j].high)
        if hod_i >= i:
            continue
        hod = session[hod_i].high
        if hod <= 0:
            continue
        drop = (hod - bar.close) / hod * 100.0
        if drop + 1e-9 < rules.hod_drop_pct:
            continue
        if bar.close >= vwap[i]:
            continue
        if not (ema_fast[i] < ema_slow[i]):
            continue
        lower = False
        for pivot in highs:
            if pivot + rules.swing_right > i or pivot <= hod_i:
                continue
            if session[pivot].high < hod - 1e-9:
                lower = True
                break
        if not lower:
            continue
        for pivot, break_at in broken.items():
            if break_at >= i:
                continue
            level = session[pivot].low
            already = False
            for k in range(break_at + 1, i):
                if session[k].high + 1e-9 >= level and session[k].close < level:
                    already = True
                    break
            if already:
                continue
            if bar.high + 1e-9 >= level and bar.close < level:
                return i
    return None


def _blend(premium: float, half_px: float | None, exit_px: float, rules: AfternoonRules) -> tuple[float, float]:
    if half_px is None or premium <= 0:
        dollars = (exit_px - premium) * 100.0 * rules.contracts
        pct = ((exit_px / premium) - 1.0) * 100.0 if premium else 0.0
        return pct, dollars
    dollars = (
        0.5 * (half_px - premium) * 100.0 + 0.5 * (exit_px - premium) * 100.0
    ) * rules.contracts
    pct = 0.5 * rules.target_pct + 0.5 * ((exit_px - premium) / premium * 100.0)
    return pct, dollars


def _walk_marks(
    premium: float,
    marks: list[tuple[datetime, float, float, float]],
    rules: AfternoonRules,
) -> dict[str, Any]:
    """Walk option open/high/low after the signal bar.

    Each mark is (time, open, high, low). High is the best put print and low
    is the worst. A bar that can stop and pay the target takes the stop.
    The half sale does not trail out on that same bar unless the put is back
    at the fill. Later bars trail when the put gives back half of the peak gain.
    """
    half_px: float | None = None
    peak = premium
    exit_px = premium
    exit_at = marks[-1][0] if marks else None
    reason = "close"
    stop_line = premium * (1.0 - rules.stop_pct / 100.0)
    target = premium * (1.0 + rules.target_pct / 100.0)
    for moment, opened, high, low in marks:
        if moment.time() >= rules.flat_ct:
            exit_px = opened
            exit_at = moment
            reason = "flat"
            break
        if half_px is None:
            if low <= stop_line + 1e-9:
                exit_px = opened if opened <= stop_line else stop_line
                exit_at = moment
                reason = "stop"
                break
            if high + 1e-9 >= target:
                half_px = opened if opened >= target else target
                peak = max(half_px, high)
                if low <= premium + 1e-9:
                    exit_px = premium
                    exit_at = moment
                    reason = "trail"
                    break
                continue
        else:
            peak = max(peak, high)
            trail = premium + rules.trail_keep * (peak - premium)
            if low <= stop_line + 1e-9:
                exit_px = opened if opened < stop_line else stop_line
                exit_at = moment
                reason = "stop"
                break
            if low <= trail + 1e-9:
                exit_px = opened if opened < trail else trail
                exit_at = moment
                reason = "trail"
                break
        exit_px = opened
        exit_at = moment
    open_trade = reason == "close"
    pct, dollars = (None, None) if open_trade else _blend(premium, half_px, exit_px, rules)
    return {
        "premium": premium,
        "half_premium": half_px,
        "exit_premium": None if open_trade else exit_px,
        "option_pct": pct,
        "pnl_usd": dollars,
        "reason": reason,
        "exit_at": exit_at,
        "priced": "yahoo" if marks else "model",
    }


def simulate(
    symbol: str,
    session: list[Bar],
    entry_index: int,
    option: list[Bar] | None = None,
    rules: AfternoonRules | None = None,
    strike: float | None = None,
) -> dict[str, Any]:
    """Mark the 0DTE put from the signal bar.

    When ``option`` is the Yahoo 5-minute series for the chosen strike, the
    entry is that bar's open and the exit walks the later put bars. That is
    the Nov 2 replay. Otherwise the premium is the Black-Scholes stand-in and
    the underlying path is revalued on each later bar.
    """
    rules = rules or PRIMARY_RULES
    entry_bar = session[entry_index]
    spot = entry_bar.close
    if strike is None or strike <= 0:
        strike = one_strike_otm_put(spot, symbol)
    else:
        strike = float(strike)
    book = _align_option(session, option)
    signal_opt = book.get(_clock_key(entry_bar))
    iv = MODEL_IV.get(symbol, 0.20)
    if signal_opt is not None and signal_opt.open > 0:
        premium = float(signal_opt.open)
        priced = "yahoo"
        marks: list[tuple[datetime, float, float, float]] = []
        for bar in session[entry_index + 1 :]:
            opt = book.get(_clock_key(bar))
            if opt is None:
                continue
            marks.append((opt.ct(), opt.open, opt.high, opt.low))
        walked = _walk_marks(premium, marks, rules)
        walked["priced"] = priced
    else:
        premium = model_put(spot, strike, _minutes_left(entry_bar.ct(), rules), iv)
        marks = []
        for bar in session[entry_index + 1 :]:
            left = _minutes_left(bar.ct(), rules)
            # A put's best print is the underlying low, and the worst is the high.
            opened = model_put(bar.open, strike, left, iv)
            best = model_put(bar.low, strike, left, iv)
            worst = model_put(bar.high, strike, left, iv)
            marks.append((bar.ct(), opened, max(opened, best, worst), min(opened, best, worst)))
        walked = _walk_marks(premium, marks, rules)
        walked["priced"] = "model"
    exit_at = walked.get("exit_at") or session[-1].ct()
    # Underlying percent is the spot change through the exit bar, not the premium.
    # An open trade has no exit premium; do not coerce that None.
    exit_spot = spot
    exit_clock = exit_at.strftime("%H:%M") if isinstance(exit_at, datetime) else ""
    for bar in session[entry_index:]:
        if bar.ct().strftime("%H:%M") == exit_clock:
            exit_spot = bar.close
            break
    und = (spot - exit_spot) / spot * 100.0 if spot else 0.0
    return {
        "symbol": symbol,
        "at": entry_bar.ct().isoformat(),
        "at_ct": entry_bar.ct().strftime("%H:%M"),
        "day": entry_bar.ct().date().isoformat(),
        "side": "P",
        "strike": strike,
        "entry": round(spot, 4),
        "premium": round(float(walked["premium"]), 4),
        "half_premium": None if walked["half_premium"] is None else round(float(walked["half_premium"]), 4),
        "exit_premium": None if walked["exit_premium"] is None else round(float(walked["exit_premium"]), 4),
        "option_pct": None if walked["option_pct"] is None else round(float(walked["option_pct"]), 4),
        "pnl_usd": None if walked["pnl_usd"] is None else round(float(walked["pnl_usd"]), 2),
        "underlying_pct": und,
        "reason": walked["reason"],
        "exit_at_ct": exit_at.strftime("%H:%M") if isinstance(exit_at, datetime) else "",
        "priced": walked["priced"],
        "contracts": rules.contracts,
        "webull": symbol in WEBULL_SYMBOLS,
    }


def run_symbol(
    symbol: str,
    bars: list[Bar],
    option_by_day: dict[date, list[Bar]] | None = None,
    rules: AfternoonRules | None = None,
) -> list[dict[str, Any]]:
    """One put per session. The first signal is the only one, stop or not."""
    rules = rules or PRIMARY_RULES
    trades: list[dict[str, Any]] = []
    for day, session in group_sessions(bars).items():
        found = first_signal(session, rules)
        if found is None:
            continue
        option = (option_by_day or {}).get(day)
        trades.append(simulate(symbol, session, found, option, rules))
    return trades


def apply_daily_cap(
    trades: list[dict[str, Any]],
    limit: int = DAILY_CAP,
) -> list[dict[str, Any]]:
    """First ``limit`` signals each day. A tie goes to SPY, then QQQ, then SPX."""
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


def plan_breadth(
    gates: dict[str, datetime],
    *,
    entry_end: time = time(14, 15),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decide which names enter.

    SPY and QQQ enter together, at the later gate, only when both gates are
    inside the entry window. A lone ETF gate waits until ``entry_end``. SPX
    keeps its own gate and does not confirm the other two.
    """
    def _aware(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            from odte_scanner.time_cst import CT

            return moment.replace(tzinfo=CT)
        return moment

    usable = {symbol: _aware(moment) for symbol, moment in gates.items() if isinstance(moment, datetime)}
    entries: dict[str, datetime] = {}
    waiting: list[str] = []
    spy = usable.get("SPY")
    qqq = usable.get("QQQ")
    if spy is not None and qqq is not None:
        confirm = spy if spy >= qqq else qqq
        if confirm.time() < entry_end and spy.time() < entry_end and qqq.time() < entry_end:
            entries["SPY"] = confirm
            entries["QQQ"] = confirm
    else:
        lone = "SPY" if spy is not None else ("QQQ" if qqq is not None else None)
        if lone is not None:
            expired = False
            if now is not None:
                clock = _aware(now)
                expired = clock.time() >= entry_end
            if not expired and usable[lone].time() < entry_end:
                waiting.append(lone)
    spx = usable.get("SPX")
    if spx is not None and spx.time() < entry_end:
        entries["SPX"] = spx
    return {"entries": entries, "waiting": waiting}


def _index_at(session: list[Bar], moment: datetime) -> int | None:
    """Last bar at or before ``moment``. Prices on that bar are the current ones."""
    target = moment
    found: int | None = None
    for i, bar in enumerate(session):
        if bar.ct() <= target:
            found = i
        else:
            break
    return found


def _option_series(
    option_bars: dict[str, list[Bar]] | None,
    symbol: str,
    strike: float,
) -> list[Bar] | None:
    if not option_bars:
        return None
    series = option_bars.get(f"{symbol}|{strike:g}")
    return series or None


def live_entries(
    bars_by_symbol: dict[str, list[Bar]],
    option_bars: dict[str, list[Bar]] | None = None,
    rules: AfternoonRules | None = None,
    *,
    now: datetime | None = None,
    strike_at: Callable[[str, float, datetime], float] | None = None,
) -> dict[str, Any]:
    """Breadth book for the live desk.

    ``replay`` stays the ungated study of each gate. This is what the desk
    enters: both ETFs at the confirming bar, or a waiting row for the first
    one. SPX is unchanged. ``strike_at`` is a research hook. The live desk
    leaves it empty, so the contract stays one strike out of the money.
    """
    rules = rules or PRIMARY_RULES
    sessions = {symbol: group_sessions(bars or []) for symbol, bars in bars_by_symbol.items()}
    days: set[date] = set()
    for book in sessions.values():
        days.update(book)
    trades: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for day in sorted(days):
        day_sessions: dict[str, list[Bar]] = {}
        gates: dict[str, datetime] = {}
        gate_index: dict[str, int] = {}
        for symbol in SYMBOLS:
            session = (sessions.get(symbol) or {}).get(day)
            if not session:
                continue
            day_sessions[symbol] = session
            found = first_signal(session, rules)
            if found is None:
                continue
            gates[symbol] = session[found].ct()
            gate_index[symbol] = found
        plan = plan_breadth(gates, entry_end=rules.entry_end, now=now)
        for symbol, entry_at in plan["entries"].items():
            session = day_sessions.get(symbol)
            if not session:
                continue
            entry_i = _index_at(session, entry_at)
            gate_i = gate_index.get(symbol, entry_i or 0)
            if entry_i is None:
                continue
            clock = session[entry_i].ct().time()
            if clock < rules.entry_start or clock >= rules.entry_end:
                continue
            spot_now = float(session[entry_i].close)
            chosen = strike_at(symbol, spot_now, session[entry_i].ct()) if strike_at is not None else None
            if chosen is None or chosen <= 0:
                strike = one_strike_otm_put(spot_now, symbol)
                option = _option_series(option_bars, symbol, strike)
                row = simulate(symbol, session, entry_i, option, rules)
            else:
                strike = float(chosen)
                option = _option_series(option_bars, symbol, strike)
                row = simulate(symbol, session, entry_i, option, rules, strike=strike)
            gate_bar = session[gate_i]
            row["gate_at"] = gate_bar.ct().isoformat()
            row["gate_at_ct"] = gate_bar.ct().strftime("%H:%M")
            row["breadth"] = "confirmed"
            trades.append(row)
        for symbol in plan["waiting"]:
            session = day_sessions.get(symbol)
            gate_i = gate_index.get(symbol)
            if session is None or gate_i is None:
                continue
            bar = session[gate_i]
            waiting.append(
                {
                    "symbol": symbol,
                    "day": day.isoformat(),
                    "gate_at": bar.ct().isoformat(),
                    "gate_at_ct": bar.ct().strftime("%H:%M"),
                    "at_ct": bar.ct().strftime("%H:%M"),
                    "side": "P",
                    "breadth": "waiting",
                    "webull": symbol in WEBULL_SYMBOLS,
                }
            )
    rank = {symbol: i for i, symbol in enumerate(PRIORITY)}
    trades.sort(key=lambda row: (str(row.get("gate_at") or row.get("at") or ""), rank.get(str(row.get("symbol") or ""), 99)))
    return {"trades": trades, "waiting": waiting}


def replay(
    bars_by_symbol: dict[str, list[Bar]],
    option_bars: dict[str, list[Bar]] | None = None,
    rules: AfternoonRules | None = None,
) -> list[dict[str, Any]]:
    """Capped book. ``option_bars`` keys are ``SYMBOL|strike`` for one day.

    A series is applied on the session that contains the signal. The Nov 2
    fixture is a single session, so the strike series lines up with that day.
    """
    rules = rules or PRIMARY_RULES
    raw: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        bars = bars_by_symbol.get(symbol) or []
        if not bars:
            continue
        # Group option series by the calendar day of their first bar. The
        # caller passes the strike that the signal is expected to choose.
        by_day: dict[date, list[Bar]] = {}
        if option_bars:
            sessions = group_sessions(bars)
            for day, session in sessions.items():
                found = first_signal(session, rules)
                if found is None:
                    continue
                strike = one_strike_otm_put(session[found].close, symbol)
                key = f"{symbol}|{strike:g}"
                series = option_bars.get(key)
                if series:
                    by_day[day] = series
        raw.extend(run_symbol(symbol, bars, by_day, rules))
    return apply_daily_cap(raw, rules.daily_cap)


def bars_from_rows(rows: list[dict[str, Any]]) -> list[Bar]:
    """Fixture rows into session bars. ``ts`` may be ISO with an offset."""
    out: list[Bar] = []
    for row in rows:
        ts = datetime.fromisoformat(str(row["ts"]))
        out.append(
            Bar(
                ts=ts,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v") or 0),
            )
        )
    return out


def load_replay_fixture(path: str) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]]]:
    import json
    from pathlib import Path

    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    underlying = {symbol: bars_from_rows(rows) for symbol, rows in (blob.get("underlying") or {}).items()}
    puts = {key: bars_from_rows(rows) for key, rows in (blob.get("puts") or {}).items()}
    return underlying, puts


# The bar object is closed five minutes after its stamp. Live cards wait for that.
BAR_MINUTES = 5


def bar_is_closed(bar: Bar, now: datetime) -> bool:
    stamp = bar.ct()
    if now.tzinfo is None:
        return False
    return stamp + timedelta(minutes=BAR_MINUTES) <= now.astimezone(stamp.tzinfo)
