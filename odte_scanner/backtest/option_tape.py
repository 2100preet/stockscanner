"""Replay desk exits on Tradier 1-minute option trade prints.

No broker import and no orders. Daily files are never read. A missing minute
is a hole, not a price. Prints are not bids or asks. Any worse-than-print
fill is labeled as an assumption.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from odte_scanner.backtest.rule_replay import (
    ROUTED,
    SCAN_SEC,
    Bar,
    Flags,
    Spec,
    TradeResult,
    metrics,
    spec_for,
    stop_fraction,
)

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

# One missing minute is allowed (Tradier often skips an empty minute).
# Anything wider is a hole: the stop and the target cannot be ordered.
MAX_GAP_SEC = 120
PRICE_TOL = 0.01
RISK_USD = 175.0


def _aware(moment: datetime, zone: ZoneInfo = ET) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=zone)
    return moment


def _parse_print_time(text: str) -> datetime:
    """Tradier timesales stamps are exchange time, America/New_York, often naive."""
    moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return _aware(moment, ET)


def load_minute_bars(root: Path, contract: str, day: str) -> tuple[list[Bar], str]:
    """Load one session of option prints. Daily files are refused."""
    if day == "daily" or contract.endswith("daily"):
        return [], "refused_daily"
    path = root / "options" / contract / f"{day}.json"
    if not path.is_file():
        return [], "no_minute_file"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return [], "unreadable"
    if not raw.lstrip().startswith(("{", "[")):
        if "symbol not found" in raw.lower():
            return [], "symbol_not_found"
        return [], "not_json"
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError:
        return [], "unreadable"
    if not isinstance(blob, dict):
        return [], "not_json_object"
    series = blob.get("series")
    rows = series.get("data") if isinstance(series, dict) else None
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        return [], "no_prints"
    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("time"):
            continue
        try:
            close = float(row["close"])
            high = float(row["high"])
            low = float(row["low"])
            opened = float(row.get("open") if row.get("open") is not None else close)
        except (TypeError, ValueError, KeyError):
            continue
        if min(close, high, low, opened) < 0:
            continue
        bars.append(Bar(ts=_parse_print_time(str(row["time"])), open=opened, high=high, low=low, close=close))
    bars.sort(key=lambda bar: bar.ts)
    if not bars:
        return [], "no_prints"
    return bars, "ok"


def minute_floor(moment: datetime) -> datetime:
    moment = _aware(moment)
    return moment.replace(second=0, microsecond=0)


def bar_at(bars: list[Bar], moment: datetime) -> Bar | None:
    target = minute_floor(moment)
    for bar in bars:
        if minute_floor(bar.ts) == target:
            return bar
    return None


def price_inside(bar: Bar, price: float, *, tol: float = PRICE_TOL) -> bool:
    return (bar.low - tol) <= float(price) <= (bar.high + tol)


def arithmetic_pnl(entry: float, exit_px: float, qty: int) -> float:
    return round((float(exit_px) - float(entry)) * 100.0 * max(1, int(qty)), 2)


def _horizon(spec: Spec, entry_ct: datetime) -> datetime | None:
    if spec.flat is None:
        return None
    return datetime(
        entry_ct.year,
        entry_ct.month,
        entry_ct.day,
        spec.flat.hour,
        spec.flat.minute,
        tzinfo=CT,
    ).astimezone(ET)


def path_status(bars: list[Bar], start: datetime, end: datetime) -> str:
    """``ok`` only when prints exist from the start minute through the end minute.

    A gap wider than two minutes is ``gap``. Ending before ``end`` is ``short``.
    """
    start_m = minute_floor(start)
    end_m = minute_floor(end)
    if start_m > end_m:
        return "entered_after_flatten"
    window = [bar for bar in bars if minute_floor(bar.ts) >= start_m]
    if not window:
        return "no_bars_after_entry"
    if bar_at(bars, start) is None:
        return "entry_minute_absent"
    if minute_floor(window[-1].ts) < end_m:
        return "ends_before_horizon"
    prev = None
    for bar in window:
        if minute_floor(bar.ts) > end_m:
            break
        if prev is not None:
            gap = (bar.ts - prev.ts).total_seconds()
            if gap > MAX_GAP_SEC:
                return "gap"
        prev = bar
    return "ok"


def _qty(entry: float, sl: float, original: int, flags: Flags) -> tuple[int, str | None]:
    """Keep the original size. Drop the trade when one lot risks more than $175.

    Never increases quantity. ``size_175`` false leaves the original count.
    """
    original = max(1, int(original or 1))
    if not flags.size_175:
        return original, None
    risk = float(entry) * float(sl) * 100.0
    if risk > RISK_USD + 1e-6:
        return 0, "risk_over_175"
    return original, None


def _exit_option(
    bars: list[Bar],
    entry_ts: datetime,
    entry_px: float,
    spec: Spec,
    flags: Flags,
) -> tuple[float, datetime, str] | None:
    """Walk prints after the entry minute. None means the tape cannot finish the rule."""
    start = minute_floor(entry_ts)
    after = [bar for bar in bars if minute_floor(bar.ts) > start]
    if not after:
        return None
    entry_day = entry_ts.astimezone(CT).date()
    stop_px = entry_px * (1.0 - spec.sl)
    target_pct = spec.tp if spec.tp is not None else spec.arm
    target_px = entry_px * (1.0 + float(target_pct))
    peak = entry_px
    working: float | None = None
    next_scan = entry_ts + timedelta(seconds=SCAN_SEC)
    for bar in after:
        clock = False
        if spec.flat is not None:
            ct = bar.ts.astimezone(CT)
            clock = ct.date() == entry_day and ct.time() >= spec.flat
        # Same-minute stop and target: the stop fills. The print does not say which came first.
        hit_stop = bar.low <= stop_px + PRICE_TOL
        hit_tp = bar.high + PRICE_TOL >= target_px and (
            spec.tp is not None or bar.high + PRICE_TOL >= entry_px * (1.0 + spec.arm)
        )
        if flags.broker_exits:
            if flags.breakeven and bar.high + PRICE_TOL >= entry_px * (1.0 + spec.arm):
                stop_px = max(stop_px, entry_px)
                hit_stop = bar.low <= stop_px + PRICE_TOL
            if hit_stop and hit_tp:
                return max(0.01, min(stop_px, bar.low)), bar.ts, "stop_ambiguous_bar"
            if hit_stop:
                # Print low, not a bid. A gap through the stop sells at the low print.
                return max(0.01, min(stop_px, bar.low)), bar.ts, "stop"
            if hit_tp and spec.tp is not None:
                return max(0.01, target_px), bar.ts, "target"
            if spec.tp is None and bar.high + PRICE_TOL >= entry_px * (1.0 + spec.arm) and flags.breakeven:
                pass
            if clock and flags.clock_on_time:
                return max(0.01, bar.close), bar.ts, "flat_print"
            if clock and bar.ts >= next_scan:
                return max(0.01, bar.close), bar.ts, "flat_print"
            if bar.ts >= next_scan:
                while next_scan <= bar.ts:
                    next_scan += timedelta(seconds=SCAN_SEC)
            continue
        if working is not None and bar.high + PRICE_TOL >= working:
            return working, bar.ts, "stop"
        if flags.clock_on_time and clock:
            return max(0.01, bar.close), bar.ts, "flat_print"
        if bar.ts < next_scan and not (clock and bar.ts >= next_scan):
            continue
        mark = bar.close
        peak = max(peak, mark)
        if flags.breakeven and peak + PRICE_TOL >= entry_px * (1.0 + spec.arm):
            stop_px = max(stop_px, entry_px)
        trail_line = entry_px + 0.5 * (peak - entry_px)
        if (
            spec.trail
            and not flags.breakeven
            and peak + PRICE_TOL >= entry_px * (1.0 + spec.arm)
            and mark < peak - PRICE_TOL
            and mark <= trail_line + PRICE_TOL
        ):
            return max(0.01, mark), bar.ts, "trail_print"
        if spec.full_at_arm and spec.tp is not None and mark + PRICE_TOL >= entry_px * (1.0 + spec.arm):
            return max(0.01, mark), bar.ts, "target_print"
        if spec.tp is not None and not spec.full_at_arm and mark + PRICE_TOL >= entry_px * (1.0 + spec.tp):
            return max(0.01, mark), bar.ts, "target_print"
        if mark <= stop_px + PRICE_TOL:
            working = stop_px
            if bar.high + PRICE_TOL >= working:
                return working, bar.ts, "stop"
        if clock:
            return max(0.01, mark), bar.ts, "flat_print"
        while next_scan <= bar.ts:
            next_scan += timedelta(seconds=SCAN_SEC)
    return None


def replay_exit_from_fill(
    bars: list[Bar],
    *,
    entry: float,
    entry_ts: datetime,
    qty: int,
    spec: Spec,
    flags: Flags,
    desk: str,
    symbol: str,
    day: str,
    contract: str,
    actual_pnl: float | None,
) -> TradeResult:
    sized, skip = _qty(entry, spec.sl, qty, flags)
    if skip:
        return TradeResult(
            desk, symbol, day, contract, False, 0, entry, None, 0.0, skip,
            entry_at=entry_ts.isoformat(), actual_pnl=actual_pnl, variant=flags.name,
        )
    done = _exit_option(bars, entry_ts, entry, spec, flags)
    if done is None:
        return TradeResult(
            desk, symbol, day, contract, False, 0, entry, None, 0.0, "tape_ended",
            entry_at=entry_ts.isoformat(), actual_pnl=actual_pnl, variant=flags.name,
        )
    exit_px, exit_ts, why = done
    pnl = arithmetic_pnl(entry, exit_px, sized)
    return TradeResult(
        desk, symbol, day, contract, True, sized, round(entry, 4), round(exit_px, 4), pnl, why,
        entry_at=entry_ts.isoformat(), exit_at=exit_ts.isoformat(), bar_seconds=60,
        actual_pnl=actual_pnl, variant=flags.name,
    )


def variant_flags() -> list[Flags]:
    """Exit and risk variants. Sizing never increases the original quantity."""
    return [
        Flags("baseline"),
        Flags("broker_stop", broker_exits=True),
        Flags("size_175", size_175=True),
        Flags("breakeven", breakeven=True),
        Flags("daily_halt", daily_halt=True),
        Flags("clock_ontime", clock_on_time=True),
        Flags(
            "combined_no_upsize",
            broker_exits=True,
            size_175=True,
            breakeven=True,
            daily_halt=True,
            clock_on_time=True,
        ),
    ]


@dataclass
class Prepared:
    desk: str
    symbol: str
    day: str
    contract: str
    qty: int
    entry: float
    exit_px: float
    entry_ts: datetime
    exit_ts: datetime
    actual_pnl: float
    expiry: date | None
    bars: list[Bar]
    entry_bar: Bar | None
    exit_bar: Bar | None
    load_status: str
    eligible: bool
    exclude_reason: str
    spec: Spec | None


def prepare_trip(trip: dict, root: Path) -> Prepared:
    entry_ts = _aware(datetime.fromisoformat(str(trip["entered_at"]).replace("Z", "+00:00")))
    exit_ts = _aware(datetime.fromisoformat(str(trip["exited_at"]).replace("Z", "+00:00")))
    day = entry_ts.astimezone(CT).date().isoformat()
    desk = str(trip.get("desk") or "")
    contract = str(trip.get("contract") or "")
    qty = int(float(trip.get("contracts") or 1))
    entry = float(trip["entry"])
    exit_px = float(trip["exit"])
    actual = float(trip["pnl"])
    expiry = None
    raw_exp = str(trip.get("expiry") or "")[:10]
    try:
        expiry = date.fromisoformat(raw_exp) if len(raw_exp) == 10 else None
    except ValueError:
        expiry = None
    spec = spec_for(desk, expiry, entry_ts.astimezone(CT).date()) if desk in ROUTED else None
    bars, status = load_minute_bars(root, contract, entry_ts.astimezone(ET).date().isoformat())
    # A hold that leaves the entry session needs that later session too. Do not stitch daily bars.
    if exit_ts.astimezone(ET).date() != entry_ts.astimezone(ET).date():
        more, more_status = load_minute_bars(root, contract, exit_ts.astimezone(ET).date().isoformat())
        if more_status != "ok":
            status = f"exit_session_{more_status}"
        else:
            bars = sorted(bars + more, key=lambda bar: bar.ts)
    entry_bar = bar_at(bars, entry_ts) if bars else None
    exit_bar = bar_at(bars, exit_ts) if bars else None
    reason = "ok"
    eligible = False
    if desk not in ROUTED or spec is None:
        reason = "not_routed"
    elif status != "ok":
        reason = status
    elif entry_bar is None:
        reason = "entry_minute_absent"
    else:
        horizon = _horizon(spec, entry_ts.astimezone(CT))
        if horizon is None:
            reason = "overnight_without_continuous_tape"
        else:
            # Need prints through the flatten even if the real exit was earlier,
            # so every variant sees the same path.
            reason = path_status(bars, entry_ts, horizon.astimezone(ET))
            if reason == "ok" and not price_inside(entry_bar, entry):
                reason = "entry_outside_print"
            eligible = reason == "ok"
    return Prepared(
        desk=desk,
        symbol=str(trip.get("symbol") or ""),
        day=day,
        contract=contract,
        qty=qty,
        entry=entry,
        exit_px=exit_px,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        actual_pnl=actual,
        expiry=expiry,
        bars=bars,
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        load_status=status,
        eligible=eligible,
        exclude_reason=reason if not eligible else "ok",
        spec=spec,
    )


def reconcile(row: Prepared) -> dict:
    """Check the recorded fill against the print, separate from any new exit rule."""
    arith = arithmetic_pnl(row.entry, row.exit_px, row.qty)
    entry_inside = None if row.entry_bar is None else price_inside(row.entry_bar, row.entry)
    exit_inside = None if row.exit_bar is None else price_inside(row.exit_bar, row.exit_px)
    return {
        "desk": row.desk,
        "symbol": row.symbol,
        "day": row.day,
        "contract": row.contract,
        "qty": row.qty,
        "entry": row.entry,
        "exit": row.exit_px,
        "recorded_pnl": row.actual_pnl,
        "arithmetic_pnl": arith,
        "arithmetic_gap": round(row.actual_pnl - arith, 2),
        "entry_minute": None if row.entry_bar is None else row.entry_bar.ts.isoformat(),
        "entry_inside_print": entry_inside,
        "exit_minute_present": row.exit_bar is not None,
        "exit_inside_print": exit_inside,
        "load_status": row.load_status,
        "eligible": row.eligible,
        "exclude_reason": row.exclude_reason,
    }


def run_variants(rows: list[Prepared]) -> dict[str, list[TradeResult]]:
    """Same eligible rows for every variant. Halts and the $175 skip change the filled count."""
    eligible = [row for row in rows if row.eligible and row.spec is not None]
    eligible.sort(key=lambda row: (row.entry_ts, row.symbol, row.contract))
    out: dict[str, list[TradeResult]] = {}
    for flags in variant_flags():
        closed: list[TradeResult] = []
        results: list[TradeResult] = []
        for row in eligible:
            halted = False
            if flags.daily_halt:
                halted = _halted(closed, row.entry_ts, row.desk)
            if halted:
                results.append(
                    TradeResult(
                        row.desk, row.symbol, row.day, row.contract, False, 0, row.entry, None, 0.0,
                        "halted", entry_at=row.entry_ts.isoformat(), actual_pnl=row.actual_pnl, variant=flags.name,
                    )
                )
                continue
            trade = replay_exit_from_fill(
                row.bars,
                entry=row.entry,
                entry_ts=row.entry_ts,
                qty=row.qty,
                spec=row.spec,
                flags=flags,
                desk=row.desk,
                symbol=row.symbol,
                day=row.day,
                contract=row.contract,
                actual_pnl=row.actual_pnl,
            )
            results.append(trade)
            if trade.filled:
                closed.append(trade)
        out[flags.name] = results
    return out


def _halted(closed: list[TradeResult], moment: datetime, desk: str) -> bool:
    day = moment.astimezone(CT).date().isoformat()
    account = 0.0
    desk_pnl = 0.0
    losses = 0
    for trade in closed:
        if not trade.filled or not trade.exit_at:
            continue
        try:
            exited = datetime.fromisoformat(trade.exit_at)
        except ValueError:
            continue
        if exited.tzinfo is None:
            exited = exited.replace(tzinfo=ET)
        exited_ct = exited.astimezone(CT)
        if exited_ct.date().isoformat() != day or exited_ct > moment.astimezone(CT):
            continue
        account += trade.pnl
        if trade.desk == desk:
            desk_pnl += trade.pnl
            if trade.pnl < 0:
                losses += 1
    if account <= -800:
        return True
    return desk_pnl <= -400 or losses >= 2


def coverage_report(rows: list[Prepared]) -> dict:
    def pack(subset: list[Prepared]) -> dict:
        by: dict[str, dict[str, int]] = {}
        for row in subset:
            cell = by.setdefault(row.desk, {"trips": 0, "eligible": 0})
            cell["trips"] += 1
            if row.eligible:
                cell["eligible"] += 1
        reasons: dict[str, int] = {}
        for row in subset:
            if row.eligible:
                continue
            reasons[row.exclude_reason] = reasons.get(row.exclude_reason, 0) + 1
        return {
            "trips": len(subset),
            "eligible": sum(1 for row in subset if row.eligible),
            "by_desk": by,
            "exclude_reasons": reasons,
        }

    focus = [row for row in rows if "2026-10-31" <= row.day <= "2026-11-06"]
    return {"full": pack(rows), "focus": pack(focus)}


def summarize_window(name: str, trades_by_variant: dict[str, list[TradeResult]], eligible: int) -> dict:
    variants = {}
    for variant, trades in trades_by_variant.items():
        stats = metrics(trades)
        stats["eligible_denominator"] = eligible
        stats["filled_of_eligible"] = f"{stats['trades']}/{eligible}"
        desks = {}
        for desk in sorted({trade.desk for trade in trades}):
            cell = metrics([trade for trade in trades if trade.desk == desk])
            cell["eligible_in_desk"] = sum(1 for trade in trades if trade.desk == desk)
            desks[desk] = cell
        variants[variant] = {"all": stats, "by_desk": desks}
    return {"window": name, "eligible": eligible, "variants": variants}
