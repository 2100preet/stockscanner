"""Replay desk rules on an underlying path. No broker, no orders.

The live process is not imported. Nothing here can place, change, or cancel
an order. Premium is a labeled proxy: a constant 0.50 delta applied to the
underlying bar, anchored so the premium equals the signal ask (or the real
fill) at the anchor print. It is not an option quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

DELTA = 0.50
HALF_SPREAD = 0.04
SCAN_SEC = 15 * 60
CANCEL_SEC = 75
HOT_LAG_CAP_SEC = 2.7 * 60
HOT_LAG_TRIGGER_SEC = 15 * 60
RISK_USD = 175.0
MAX_CONTRACTS = 5
DESK_HALT_USD = -400.0
DESK_HALT_LOSERS = 2
ACCOUNT_HALT_USD = -800.0

HOT_DESKS = frozenset({"Sniper", "Diamond", "Afternoon Puts"})
ROUTED = frozenset({"Sniper", "Diamond", "Afternoon Puts", "Gold Setup"})


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Idea:
    """One desk signal. ``signal_ask`` is an ask when ``quote_kind`` is ask."""

    desk: str
    symbol: str
    right: str
    day: str
    signaled_at: datetime
    seen_at: datetime
    signal_ask: float
    spot: float
    contract: str
    quote_kind: str = "ask"
    expiry: date | None = None
    strike: float | None = None
    actual_entry: float | None = None
    actual_entry_at: datetime | None = None
    actual_pnl: float | None = None
    source: str = "ledger"


@dataclass(frozen=True)
class Spec:
    sl: float
    arm: float
    tp: float | None
    flat: time | None
    entry_end: time
    full_at_arm: bool
    trail: bool


@dataclass(frozen=True)
class Flags:
    name: str
    broker_exits: bool = False
    size_175: bool = False
    breakeven: bool = False
    daily_halt: bool = False
    ask_entry: bool = False
    clock_on_time: bool = False
    entry_1min: bool = False
    use_actual_fill: bool = False


@dataclass
class TradeResult:
    desk: str
    symbol: str
    day: str
    contract: str
    filled: bool
    qty: int
    entry: float | None
    exit: float | None
    pnl: float
    reason: str
    entry_at: str | None = None
    exit_at: str | None = None
    bar_seconds: int | None = None
    actual_pnl: float | None = None
    variant: str = ""


def stop_fraction(desk: str, expiry: str | None, entry_day: str) -> float:
    """Same percents the live book uses. Same-day Gold is −40%, later Gold is −50%."""
    if desk == "Gold Setup" and expiry and expiry[:10] != entry_day:
        return 0.50
    return 0.40


def realized_size_screen(trips: list[dict], *, max_contracts: int = MAX_CONTRACTS) -> dict:
    """Apply the $175 rule to real fills. No price path and no new trades.

    ``skipped`` drops any fill whose 1-lot stop risks more than $175.
    ``scaled`` also multiplies the kept fill by floor($175 / risk), capped at
    ``max_contracts``. Same percent on extra contracts is an assumption.
    """

    def entry_day(trip: dict) -> str:
        raw = str(trip.get("entered_at") or trip.get("exited_at") or "")
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CT)
        return moment.astimezone(CT).date().isoformat()

    kept_pnl = 0.0
    skipped_pnl = 0.0
    scaled_pnl = 0.0
    kept = skipped = 0
    for trip in trips:
        try:
            entry = float(trip["entry"])
            pnl = float(trip["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        day = entry_day(trip)
        risk = entry * stop_fraction(str(trip.get("desk") or ""), str(trip.get("expiry") or ""), day) * 100.0
        if risk > RISK_USD + 1e-6:
            skipped += 1
            skipped_pnl += pnl
            continue
        qty = max(1, int(RISK_USD // risk)) if risk > 0 else 1
        qty = min(max_contracts, qty)
        kept += 1
        kept_pnl += pnl
        scaled_pnl += pnl * qty
    actual = kept_pnl + skipped_pnl
    return {
        "fills": kept + skipped,
        "actual_pnl": round(actual, 2),
        "kept": kept,
        "kept_pnl": round(kept_pnl, 2),
        "skipped": skipped,
        "skipped_pnl": round(skipped_pnl, 2),
        "skip_only_pnl": round(kept_pnl, 2),
        "scaled_pnl": round(scaled_pnl, 2),
        "note": (
            "Real Webull paper fills only. Skipping removes both the large losers "
            "and the large winners. Scaling assumes extra contracts get the same "
            "percent, which a 15-cent option usually cannot do up to a $175 stop."
        ),
    }


def variants() -> list[Flags]:
    """Baseline, each proposed change alone, the full package, and the fill sanity path."""
    base = Flags(name="baseline")
    return [
        base,
        Flags(name="broker_stop", broker_exits=True),
        Flags(name="size_175", size_175=True),
        Flags(name="breakeven", breakeven=True),
        Flags(name="daily_halt", daily_halt=True),
        Flags(name="ask_entry", ask_entry=True),
        Flags(name="clock_ontime", clock_on_time=True),
        Flags(name="entry_1min", entry_1min=True),
        Flags(
            name="proposed",
            broker_exits=True,
            size_175=True,
            breakeven=True,
            daily_halt=True,
            ask_entry=True,
            clock_on_time=True,
            entry_1min=True,
        ),
        Flags(name="baseline_on_fills", use_actual_fill=True),
    ]


def spec_for(desk: str, expiry: date | None, day: date) -> Spec | None:
    """Live exit numbers. A 1-lot cannot scale, so Gold's half-sale is not a fill."""
    if desk == "Sniper":
        return Spec(0.40, 0.25, 0.25, time(10, 30), time(10, 30), True, True)
    if desk == "Diamond":
        return Spec(0.40, 0.30, 0.30, time(10, 30), time(10, 30), True, True)
    if desk == "Afternoon Puts":
        # One contract does not sell at +100%. It trails after that print.
        return Spec(0.40, 1.00, None, time(14, 45), time(14, 15), False, True)
    if desk == "Gold Setup":
        same = bool(expiry is not None and expiry == day)
        dte = None if expiry is None else (expiry - day).days
        short = same or (dte is not None and dte <= 1)
        sl = 0.40 if same else 0.50
        flat = time(14, 30) if same else None
        # 0DTE entries stop at 15:00 ET (14:00 CT). Later entries stop at 15:45 ET.
        entry_end = time(14, 0) if same else time(14, 45)
        if short:
            return Spec(sl, 0.30, 0.30, flat, entry_end, True, True)
        return Spec(sl, 0.30, 0.80, flat, entry_end, False, True)
    return None


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=ET)
    return moment


def _ct(moment: datetime) -> datetime:
    return _aware(moment).astimezone(CT)


def _et(moment: datetime) -> datetime:
    return _aware(moment).astimezone(ET)


def action_at(idea: Idea, flags: Flags, step_sec: int) -> datetime:
    """When the desk is allowed to send the buy.

    Hot-desk ledger stamps that are more than 15 minutes after the bar are the
    slow scorecard write. The orders in this sample were sent a median 2.7
    minutes after the bar, so that cap is the delay those desks actually had.
    """
    if flags.entry_1min:
        return idea.signaled_at + timedelta(seconds=max(60, int(step_sec)))
    lag = (_aware(idea.seen_at) - _aware(idea.signaled_at)).total_seconds()
    if lag > HOT_LAG_TRIGGER_SEC and idea.desk in HOT_DESKS:
        lag = HOT_LAG_CAP_SEC
    return _aware(idea.signaled_at) + timedelta(seconds=max(0.0, lag))


def _signal_ask(idea: Idea) -> float:
    ask = float(idea.signal_ask)
    if idea.quote_kind == "mid" and ask > 0:
        return ask / (1.0 - HALF_SPREAD)
    return ask


def _premium(spot: float, spot0: float, prem0: float, right: str) -> float:
    sign = 1.0 if str(right).upper().startswith("C") else -1.0
    return max(0.01, float(prem0) + DELTA * sign * (float(spot) - float(spot0)))


def _bar_premiums(bar: Bar, spot0: float, prem0: float, right: str) -> tuple[float, float, float]:
    """Return close, low, high of the proxy premium. Low is the cheap print."""
    close = _premium(bar.close, spot0, prem0, right)
    up = _premium(bar.high, spot0, prem0, right)
    down = _premium(bar.low, spot0, prem0, right)
    return close, min(up, down), max(up, down)


def _step_seconds(bars: list[Bar]) -> int:
    if len(bars) < 2:
        return 60
    gaps = []
    for left, right in zip(bars, bars[1:]):
        gap = int((right.ts - left.ts).total_seconds())
        if 0 < gap <= 600:
            gaps.append(gap)
    if not gaps:
        return 60
    gaps.sort()
    return gaps[len(gaps) // 2]


def _session_bars(bars: Iterable[Bar]) -> list[Bar]:
    out: list[Bar] = []
    for bar in bars:
        moment = _et(bar.ts)
        if moment.weekday() >= 5:
            continue
        clock = moment.time()
        if time(9, 30) <= clock <= time(16, 0):
            out.append(bar)
    out.sort(key=lambda bar: bar.ts)
    return out


def _index_at(bars: list[Bar], moment: datetime) -> int | None:
    target = _aware(moment)
    for index, bar in enumerate(bars):
        if _aware(bar.ts) >= target:
            return index
    return None


def _spot_at(bars: list[Bar], moment: datetime, fallback: float) -> float:
    target = _aware(moment)
    prior = None
    for bar in bars:
        if _aware(bar.ts) <= target:
            prior = bar.close
        else:
            break
    if prior is not None and prior > 0:
        return float(prior)
    return float(fallback)


def _bid(premium: float) -> float:
    return max(0.01, premium * (1.0 - HALF_SPREAD))


def _qty_for(entry: float, sl: float, flags: Flags) -> tuple[int, str | None]:
    if entry <= 0:
        return 0, "no_entry_price"
    if not flags.size_175:
        return 1, None
    risk = float(entry) * float(sl) * 100.0
    if risk > RISK_USD + 1e-6:
        return 0, "risk_over_175"
    contracts = max(1, int(RISK_USD // risk)) if risk > 0 else 1
    return min(MAX_CONTRACTS, contracts), None


def _clock_due(spec: Spec, bar: Bar, entry_day: date) -> bool:
    if spec.flat is None:
        return False
    moment = _ct(bar.ts)
    return moment.date() == entry_day and moment.time() >= spec.flat


def _expired(idea: Idea, bar: Bar) -> bool:
    if idea.expiry is None:
        return False
    return _et(bar.ts).date() > idea.expiry


def _intrinsic(idea: Idea, spot: float) -> float:
    if idea.strike is None or spot <= 0:
        return 0.01
    if str(idea.right).upper().startswith("C"):
        return max(0.01, spot - float(idea.strike))
    return max(0.01, float(idea.strike) - spot)


def _exit(
    idea: Idea,
    bars: list[Bar],
    entry_index: int,
    entry_px: float,
    spot0: float,
    spec: Spec,
    flags: Flags,
) -> tuple[float, datetime, str]:
    """Walk the path after the fill. Baseline looks up on the scan grid."""
    entry_day = _ct(bars[entry_index].ts).date()
    stop_px = entry_px * (1.0 - spec.sl)
    target_pct = spec.tp if spec.tp is not None else spec.arm
    target_px = entry_px * (1.0 + target_pct)
    peak = entry_px
    working_stop: float | None = None
    next_scan = _aware(bars[entry_index].ts) + timedelta(seconds=SCAN_SEC)
    last_px = entry_px
    last_ts = bars[entry_index].ts
    last_spot = spot0

    for bar in bars[entry_index + 1 :]:
        if _expired(idea, bar):
            return max(0.01, _intrinsic(idea, last_spot)), bar.ts, "expiry"
        close, low, high = _bar_premiums(bar, spot0, entry_px, idea.right)
        last_px, last_ts, last_spot = close, bar.ts, bar.close
        clock = _clock_due(spec, bar, entry_day)
        if flags.broker_exits:
            if flags.breakeven and high + 1e-9 >= entry_px * (1.0 + spec.arm):
                stop_px = max(stop_px, entry_px)
            hit_stop = low <= stop_px + 1e-9
            hit_tp = spec.tp is not None and high + 1e-9 >= target_px
            if spec.tp is None and high + 1e-9 >= entry_px * (1.0 + spec.arm):
                hit_tp = True
                target_px = entry_px * (1.0 + spec.arm)
            if hit_stop:
                # A gap through the stop fills at the cheap print, not the stop.
                return max(0.01, min(stop_px, low)), bar.ts, "stop"
            if hit_tp:
                return max(0.01, target_px), bar.ts, "target"
            if clock and flags.clock_on_time:
                return max(0.01, _bid(close)), bar.ts, "flat"
            if clock and _aware(bar.ts) >= next_scan:
                # The resting stop does not by itself make the clock punctual.
                return max(0.01, _bid(close)), bar.ts, "flat"
            if _aware(bar.ts) >= next_scan:
                while next_scan <= _aware(bar.ts):
                    next_scan += timedelta(seconds=SCAN_SEC)
            continue

        if working_stop is not None and high + 1e-9 >= working_stop:
            return working_stop, bar.ts, "stop"
        due = _aware(bar.ts) >= next_scan
        if flags.clock_on_time and clock:
            return max(0.01, _bid(close)), bar.ts, "flat"
        if not due and not (clock and _aware(bar.ts) >= next_scan):
            continue
        mark = close
        peak = max(peak, mark)
        if flags.breakeven and peak + 1e-9 >= entry_px * (1.0 + spec.arm):
            stop_px = max(stop_px, entry_px)
        trail_line = entry_px + 0.5 * (peak - entry_px)
        if (
            spec.trail
            and not flags.breakeven
            and peak + 1e-9 >= entry_px * (1.0 + spec.arm)
            and mark < peak - 1e-9
            and mark <= trail_line + 1e-9
        ):
            return _bid(mark), bar.ts, "trail"
        if spec.full_at_arm and spec.tp is not None and mark + 1e-9 >= entry_px * (1.0 + spec.arm):
            return _bid(mark), bar.ts, "target"
        if spec.tp is not None and not spec.full_at_arm and mark + 1e-9 >= entry_px * (1.0 + spec.tp):
            return _bid(mark), bar.ts, "target"
        if mark <= stop_px + 1e-9:
            working_stop = stop_px
            if high + 1e-9 >= working_stop:
                return working_stop, bar.ts, "stop"
        if clock:
            return max(0.01, _bid(mark)), bar.ts, "flat"
        while next_scan <= _aware(bar.ts):
            next_scan += timedelta(seconds=SCAN_SEC)

    if idea.expiry is not None and _et(last_ts).date() >= idea.expiry:
        return max(0.01, _intrinsic(idea, last_spot)), last_ts, "expiry"
    return max(0.01, _bid(last_px)), last_ts, "horizon"


def _try_entry(
    idea: Idea,
    bars: list[Bar],
    flags: Flags,
    step_sec: int,
    spot0: float,
    ask0: float,
) -> tuple[int, float, datetime] | None:
    if flags.use_actual_fill and idea.actual_entry and idea.actual_entry > 0 and idea.actual_entry_at:
        index = _index_at(bars, idea.actual_entry_at)
        if index is None:
            return None
        return index, float(idea.actual_entry), _aware(idea.actual_entry_at)

    start = action_at(idea, flags, step_sec)
    begin = _index_at(bars, start)
    if begin is None:
        return None
    entry_deadline = _ct(start)
    # Do not buy before the option session, and do not buy after the desk window.
    window_end = spec_for(idea.desk, idea.expiry, _ct(idea.signaled_at).date())
    if window_end is None:
        return None

    if flags.ask_entry:
        cap = ask0 * 1.10
        for index in range(begin, len(bars)):
            bar = bars[index]
            moment = _ct(bar.ts)
            if moment.date() != entry_deadline.date():
                break
            if moment.time() > window_end.entry_end:
                break
            if _et(bar.ts).time() < time(9, 30):
                continue
            close, _, _ = _bar_premiums(bar, spot0, ask0, idea.right)
            ask_now = close * (1.0 + HALF_SPREAD)
            if ask_now <= cap + 1e-9:
                return index, ask_now, bar.ts
        return None

    limit = None
    deadline = start + timedelta(seconds=CANCEL_SEC)
    for index in range(begin, len(bars)):
        bar = bars[index]
        if _aware(bar.ts) > deadline:
            break
        moment = _ct(bar.ts)
        if moment.date() != entry_deadline.date() or moment.time() > window_end.entry_end:
            break
        if _et(bar.ts).time() < time(9, 30):
            continue
        close, low, _ = _bar_premiums(bar, spot0, ask0, idea.right)
        if limit is None:
            limit = close * (1.0 - HALF_SPREAD)
        if low <= limit + 1e-9:
            return index, float(limit), bar.ts
    return None


def _blank(idea: Idea, flags: Flags, reason: str, step: int | None = None) -> TradeResult:
    return TradeResult(
        desk=idea.desk,
        symbol=idea.symbol,
        day=idea.day,
        contract=idea.contract,
        filled=False,
        qty=0,
        entry=None,
        exit=None,
        pnl=0.0,
        reason=reason,
        bar_seconds=step,
        actual_pnl=idea.actual_pnl,
        variant=flags.name,
    )


def simulate_idea(
    idea: Idea,
    bars: list[Bar],
    flags: Flags,
    *,
    halted: bool = False,
) -> TradeResult:
    """One idea under one rule set. ``halted`` skips the entry and still returns a row."""
    if idea.desk not in ROUTED:
        return _blank(idea, flags, "not_routed")
    day = _ct(idea.signaled_at).date()
    spec = spec_for(idea.desk, idea.expiry, day)
    if spec is None:
        return _blank(idea, flags, "no_spec")
    path = _session_bars(bars)
    if not path:
        return _blank(idea, flags, "no_bars")
    if halted:
        return _blank(idea, flags, "halted", _step_seconds(path))
    ask0 = _signal_ask(idea)
    if ask0 <= 0:
        return _blank(idea, flags, "no_ask")
    spot0 = _spot_at(path, idea.signaled_at, idea.spot)
    if spot0 <= 0:
        return _blank(idea, flags, "no_spot")
    step = _step_seconds(path)
    chosen = _try_entry(idea, path, flags, step, spot0, ask0)
    if chosen is None:
        return _blank(idea, flags, "no_fill", step)
    index, entry_px, entry_ts = chosen
    # Re-anchor the delta path on the fill so the premium starts at the price paid.
    fill_spot = path[index].close
    qty, skip = _qty_for(entry_px, spec.sl, flags)
    if skip:
        return _blank(idea, flags, skip, step)
    exit_px, exit_ts, why = _exit(idea, path, index, entry_px, fill_spot, spec, flags)
    pnl = (exit_px - entry_px) * 100.0 * qty
    return TradeResult(
        desk=idea.desk,
        symbol=idea.symbol,
        day=idea.day,
        contract=idea.contract,
        filled=True,
        qty=qty,
        entry=round(entry_px, 4),
        exit=round(exit_px, 4),
        pnl=round(pnl, 2),
        reason=why,
        entry_at=entry_ts.isoformat(),
        exit_at=exit_ts.isoformat(),
        bar_seconds=step,
        actual_pnl=idea.actual_pnl,
        variant=flags.name,
    )


def _halted(closed: list[TradeResult], moment: datetime) -> bool:
    day = _ct(moment).date().isoformat()
    desk_pnl: dict[str, float] = {}
    desk_losses: dict[str, int] = {}
    account = 0.0
    for trade in closed:
        if not trade.filled or not trade.exit_at:
            continue
        try:
            exited = _ct(datetime.fromisoformat(trade.exit_at))
        except ValueError:
            continue
        if exited.date().isoformat() != day or exited > _ct(moment):
            continue
        account += trade.pnl
        desk_pnl[trade.desk] = desk_pnl.get(trade.desk, 0.0) + trade.pnl
        if trade.pnl < 0:
            desk_losses[trade.desk] = desk_losses.get(trade.desk, 0) + 1
    if account <= ACCOUNT_HALT_USD:
        return True
    return False


def _desk_halted(closed: list[TradeResult], moment: datetime, desk: str) -> bool:
    if _halted(closed, moment):
        return True
    day = _ct(moment).date().isoformat()
    pnl = 0.0
    losses = 0
    for trade in closed:
        if not trade.filled or trade.desk != desk or not trade.exit_at:
            continue
        try:
            exited = _ct(datetime.fromisoformat(trade.exit_at))
        except ValueError:
            continue
        if exited.date().isoformat() != day or exited > _ct(moment):
            continue
        pnl += trade.pnl
        if trade.pnl < 0:
            losses += 1
    return pnl <= DESK_HALT_USD or losses >= DESK_HALT_LOSERS


def run_variant(ideas: list[Idea], bars_by_symbol: dict[str, list[Bar]], flags: Flags) -> list[TradeResult]:
    """Replay every routed idea. Halts apply only when that flag is on."""
    ordered = sorted(ideas, key=lambda idea: (_aware(idea.signaled_at), idea.symbol, idea.contract))
    closed: list[TradeResult] = []
    results: list[TradeResult] = []
    for idea in ordered:
        bars = bars_by_symbol.get(idea.symbol) or []
        halted = False
        if flags.daily_halt and idea.desk in ROUTED:
            when = idea.actual_entry_at if flags.use_actual_fill and idea.actual_entry_at else action_at(
                idea, flags, _step_seconds(_session_bars(bars)) if bars else 60
            )
            halted = _desk_halted(closed, when, idea.desk)
        trade = simulate_idea(idea, bars, flags, halted=halted)
        results.append(trade)
        if trade.filled:
            closed.append(trade)
    return results


def metrics(trades: list[TradeResult]) -> dict:
    filled = [trade for trade in trades if trade.filled]
    wins = [trade.pnl for trade in filled if trade.pnl > 0]
    losses = [trade.pnl for trade in filled if trade.pnl < 0]
    gross_win = sum(wins)
    gross_loss = sum(losses)
    by_day: dict[str, float] = {}
    for trade in filled:
        stamp = trade.exit_at or trade.entry_at or trade.day
        try:
            day = _ct(datetime.fromisoformat(stamp)).date().isoformat()
        except ValueError:
            day = trade.day
        by_day[day] = by_day.get(day, 0.0) + trade.pnl
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve: list[dict[str, float | str]] = []
    worst_day = None
    worst_pnl = None
    for day in sorted(by_day):
        equity = round(equity + by_day[day], 2)
        peak = max(peak, equity)
        max_dd = min(max_dd, round(equity - peak, 2))
        curve.append({"day": day, "pnl": round(by_day[day], 2), "equity": equity})
        if worst_pnl is None or by_day[day] < worst_pnl:
            worst_pnl = by_day[day]
            worst_day = day
    profit_factor = None
    if gross_loss < 0:
        profit_factor = round(gross_win / abs(gross_loss), 3)
    return {
        "trades": len(filled),
        "skipped": len(trades) - len(filled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(filled), 3) if filled else None,
        "pnl": round(sum(trade.pnl for trade in filled), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "worst_day": worst_day,
        "worst_day_pnl": None if worst_pnl is None else round(worst_pnl, 2),
        "curve": curve,
    }


def summarize(
    ideas: list[Idea],
    bars_by_symbol: dict[str, list[Bar]],
    *,
    focus_start: str = "2026-10-31",
    focus_end: str = "2026-11-06",
    full_start: str = "2026-10-16",
    full_end: str = "2026-11-06",
) -> dict:
    """Run every variant on the focus window and the full window."""

    def in_window(idea: Idea, start: str, end: str) -> bool:
        return start <= idea.day <= end

    out: dict = {"windows": {}, "sanity": {}}
    for label, start, end in (("focus", focus_start, focus_end), ("full", full_start, full_end)):
        chosen = [idea for idea in ideas if in_window(idea, start, end)]
        window: dict = {"ideas": len(chosen), "variants": {}, "by_desk": {}}
        for flags in variants():
            trades = run_variant(chosen, bars_by_symbol, flags)
            window["variants"][flags.name] = metrics(trades)
            desks: dict = {}
            for desk in sorted({trade.desk for trade in trades}):
                desks[desk] = metrics([trade for trade in trades if trade.desk == desk])
            window["by_desk"][flags.name] = desks
            if flags.name in {"baseline", "proposed", "baseline_on_fills"}:
                window.setdefault("trades", {})[flags.name] = [
                    {
                        "desk": trade.desk,
                        "symbol": trade.symbol,
                        "day": trade.day,
                        "contract": trade.contract,
                        "filled": trade.filled,
                        "qty": trade.qty,
                        "entry": trade.entry,
                        "exit": trade.exit,
                        "pnl": trade.pnl,
                        "reason": trade.reason,
                        "entry_at": trade.entry_at,
                        "exit_at": trade.exit_at,
                        "bar_seconds": trade.bar_seconds,
                        "actual_pnl": trade.actual_pnl,
                    }
                    for trade in trades
                ]
        out["windows"][label] = window
    sanity_trades = [
        trade
        for trade in run_variant(ideas, bars_by_symbol, Flags(name="baseline_on_fills", use_actual_fill=True))
        if trade.actual_pnl is not None and trade.filled
    ]
    actual = round(sum(float(trade.actual_pnl or 0) for trade in sanity_trades), 2)
    modeled = round(sum(trade.pnl for trade in sanity_trades), 2)
    out["sanity"] = {
        "matched_fills": len(sanity_trades),
        "actual_pnl": actual,
        "baseline_exit_pnl": modeled,
        "error": round(modeled - actual, 2),
        "note": (
            "Same actual paper entries, baseline exit rules on the delta proxy. "
            "This is the check against realized P&L. It is not option-bar replay."
        ),
    }
    return out
