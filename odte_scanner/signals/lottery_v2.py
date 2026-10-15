"""Lottery v2 — direction-of-break 0–1 DTE tickets. LOG ONLY.

Puts when price holds below the opening range and VWAP. Calls when it holds
above both. This module never routes to Webull and never emits BUY_NOW, so
the desk toast regex does not treat a ticket as an order.

The stop is the premium. The exit ladder (half at 5×, rest at 10× or a 50%
giveback from the peak close, else a 15:45 ET time exit) is recorded on the
ticket for the log and for replay. It is not sent to a broker.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from odte_scanner.signals.rip_radar import MEGA_RIP_SYMBOLS

ET = ZoneInfo("America/New_York")

INDEX_NAMES = ("SPY", "QQQ", "IWM", "DIA")


def lottery_v2_universe(focus_names: list[str] | None = None) -> set[str]:
    names = set(MEGA_RIP_SYMBOLS) | set(INDEX_NAMES)
    for raw in focus_names or []:
        sym = str(raw or "").upper().strip()
        if sym:
            names.add(sym)
    return names


@dataclass
class LotteryV2Rules:
    enabled: bool = True
    min_ask: float = 0.05
    max_ask: float = 0.40
    max_dte: int = 1
    min_volume: int = 50
    min_oi: int = 100
    max_spread_pct: float = 45.0
    min_confirms: int = 3
    max_tickets: int = 3
    size_usd: float = 50.0
    size_usd_rich: float = 25.0
    cheap_ask: float = 0.20
    entry_start_et: str = "09:35"
    entry_end_et: str = "15:30"
    time_exit_et: str = "15:45"
    half_multiple: float = 5.0
    rest_multiple: float = 10.0
    giveback_from_peak: float = 0.50
    vix_call_max: float = 20.0
    vix_put_min: float = 16.0
    rel_volume_min: float = 1.1
    log_only: bool = True
    log_path: str = "outputs/lottery_v2_log.json"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rules_from_config(cfg: dict[str, Any] | None) -> LotteryV2Rules:
    src = cfg or {}
    known = {f.name for f in LotteryV2Rules.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {}
    for key, value in src.items():
        name = str(key)
        if name.startswith("lottery_v2_"):
            name = name[len("lottery_v2_") :]
        if name in known and value is not None:
            kwargs[name] = value
    return LotteryV2Rules(**kwargs)


@dataclass
class LotteryV2Ticket:
    action: str  # LOG | SKIP
    symbol: str
    right: str  # C | P
    headline: str
    detail: str
    side: str = ""  # call | put | ""
    confirms: int = 0
    confirm_tags: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    contract: str | None = None
    strike: float | None = None
    expiry: str | None = None
    ask: float | None = None
    bid: float | None = None
    dte: int | None = None
    size_usd: float | None = None
    contracts: int = 0
    log_only: bool = True
    routed: bool = False
    exit_plan: str = ""
    orb_high: float | None = None
    orb_low: float | None = None
    vwap: float | None = None
    last: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _i(value: Any) -> int | None:
    num = _f(value)
    if num is None:
        return None
    return int(num)


def _hhmm(text: str) -> time:
    hour, minute = str(text or "00:00").split(":", 1)
    return time(int(hour), int(minute))


def _as_et(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def entry_window_open(now: datetime | None, rules: LotteryV2Rules) -> bool:
    """True from entry_start inclusive through entry_end exclusive, weekdays."""
    et = _as_et(now)
    if et.weekday() >= 5:
        return False
    start = _hhmm(rules.entry_start_et)
    end = _hhmm(rules.entry_end_et)
    clock = et.timetz().replace(tzinfo=None)
    return start <= clock < end


def _flow_side(sentiment: Any) -> str | None:
    text = str(sentiment or "").strip().lower()
    if text in {"bullish", "bull", "call", "calls"}:
        return "call"
    if text in {"bearish", "bear", "put", "puts"}:
        return "put"
    return None


def ticket_budget(ask: float, rules: LotteryV2Rules) -> tuple[int, float]:
    """Contracts and dollars for one ticket inside the $25–$50 band.

    A single contract is bought when it fits under $50. Cheaper asks use the
    $50 budget; asks above ``cheap_ask`` step down toward $25, then step back
    up only far enough to buy one contract.
    """
    if ask <= 0:
        return 0, 0.0
    cost = ask * 100.0
    if cost > max(rules.size_usd, rules.size_usd_rich) + 1e-9:
        return 0, 0.0
    budget = rules.size_usd if ask <= rules.cheap_ask else rules.size_usd_rich
    if cost > budget:
        budget = min(max(rules.size_usd, rules.size_usd_rich), max(budget, cost))
    contracts = max(1, int(budget // cost))
    while contracts > 1 and contracts * cost > budget + 1e-9:
        contracts -= 1
    spent = round(contracts * cost, 2)
    return contracts, spent


def _exit_plan(rules: LotteryV2Rules) -> str:
    giveback = int(round(rules.giveback_from_peak * 100))
    return (
        f"LOG ONLY — not routed. Sell half at {rules.half_multiple:.0f}×, "
        f"rest at {rules.rest_multiple:.0f}× or {giveback}% giveback from the peak close. "
        f"Hard time exit {rules.time_exit_et} ET. Stop is the premium."
    )


def decide_lottery_v2(
    candidate: dict[str, Any],
    *,
    tape: dict[str, Any] | None,
    rules: LotteryV2Rules | None = None,
    now: datetime | None = None,
    universe: set[str] | None = None,
    vix: float | None = None,
    vix_rising: bool | None = None,
    gex: float | None = None,
    gex_regime: str | None = None,
    flow_sentiment: str | None = None,
) -> LotteryV2Ticket:
    """Gate one contract. LOG when the break holds and confirms clear. Else SKIP."""
    rules = rules or LotteryV2Rules()
    symbol = str(candidate.get("symbol") or "").upper()
    right = str(candidate.get("right") or "C").upper()[:1]
    if right not in {"C", "P"}:
        right = "C"
    ask = _f(candidate.get("ask"))
    bid = _f(candidate.get("bid"))
    dte = _i(candidate.get("dte"))
    strike = _f(candidate.get("strike"))
    volume = _i(candidate.get("volume")) or 0
    oi = _i(candidate.get("open_interest")) or 0
    tape = tape or {}
    last = _f(tape.get("last"))
    orb_high = _f(tape.get("orb_high"))
    orb_low = _f(tape.get("orb_low"))
    vwap = _f(tape.get("vwap"))
    rel_volume = _f(tape.get("rel_volume"))

    def skipped(reason: str, *, side: str = "", confirms: int = 0, tags: list[str] | None = None) -> LotteryV2Ticket:
        return LotteryV2Ticket(
            action="SKIP",
            symbol=symbol,
            right=right,
            side=side,
            headline=f"SKIP {symbol} {('CALL' if right == 'C' else 'PUT')}",
            detail=reason,
            confirms=confirms,
            confirm_tags=list(tags or []),
            vetoes=[reason],
            contract=candidate.get("contract") or None,
            strike=strike,
            expiry=candidate.get("expiry"),
            ask=ask,
            bid=bid,
            dte=dte,
            log_only=True,
            routed=False,
            exit_plan=_exit_plan(rules),
            orb_high=orb_high,
            orb_low=orb_low,
            vwap=vwap,
            last=last,
        )

    if universe is not None and symbol not in universe:
        return skipped("outside lottery v2 universe")
    if ask is None or ask < rules.min_ask or ask > rules.max_ask:
        return skipped(f"ask {ask} outside ${rules.min_ask:.2f}–${rules.max_ask:.2f}")
    if dte is None or dte < 0 or dte > rules.max_dte:
        return skipped(f"DTE {dte} outside 0–{rules.max_dte}")
    if volume < rules.min_volume and oi < rules.min_oi:
        return skipped(f"thin contract vol {volume} / oi {oi}")
    if bid is not None and bid > 0 and ask > 0:
        spread_pct = (ask - bid) / ask * 100.0
        if spread_pct > rules.max_spread_pct:
            return skipped(f"spread {spread_pct:.0f}% wider than {rules.max_spread_pct:.0f}%")
    if not entry_window_open(now, rules):
        et = _as_et(now)
        return skipped(
            f"outside entry window {rules.entry_start_et}–{rules.entry_end_et} ET "
            f"(now {et.strftime('%H:%M')} ET)"
        )
    if orb_high is None or orb_low is None or vwap is None or last is None:
        return skipped("missing ORB15 or VWAP — no held-break tape")
    if orb_high <= orb_low or vwap <= 0 or last <= 0:
        return skipped("ORB15 or VWAP is not usable")

    call_hold = last > orb_high and last > vwap
    put_hold = last < orb_low and last < vwap
    if call_hold and right == "C":
        side = "call"
    elif put_hold and right == "P":
        side = "put"
    elif call_hold or put_hold:
        held = "call" if call_hold else "put"
        return skipped(f"tape holds {held} but contract is {'call' if right == 'C' else 'put'}", side=held)
    else:
        return skipped("price is not holding beyond ORB15 and VWAP")

    tags = ["orb15_hold", "vwap_side"]
    flow = _flow_side(flow_sentiment)
    if flow is not None and flow != side:
        return skipped(f"flow sentiment {flow_sentiment} opposes {side}", side=side, confirms=2, tags=tags)

    if rel_volume is not None and rel_volume >= rules.rel_volume_min:
        tags.append("volume_thrust")
    elif volume >= rules.min_volume:
        tags.append("option_volume")

    vix_v = _f(vix)
    if vix_v is not None:
        if side == "call" and vix_v < rules.vix_call_max:
            tags.append("vix_regime")
        elif side == "put" and (vix_v >= rules.vix_put_min or vix_rising is True):
            tags.append("vix_regime")
    elif side == "put" and vix_rising is True:
        tags.append("vix_regime")

    regime = str(gex_regime or "").upper()
    gex_v = _f(gex)
    short_gamma = regime in {"SHORT_GAMMA", "NEGATIVE", "FLIPPED"} or (gex_v is not None and gex_v <= 0)
    if short_gamma:
        tags.append("gex_flip")

    if flow == side:
        tags.append("flow_side")

    # ORB + VWAP are the first two. Need one more independent confirm.
    if len(tags) < rules.min_confirms:
        return skipped(
            f"confirms {len(tags)} < {rules.min_confirms} ({', '.join(tags)})",
            side=side,
            confirms=len(tags),
            tags=tags,
        )

    contracts, spent = ticket_budget(ask, rules)
    if contracts < 1:
        return skipped("one contract does not fit the $25–$50 ticket", side=side, confirms=len(tags), tags=tags)

    word = "CALL" if side == "call" else "PUT"
    return LotteryV2Ticket(
        action="LOG",
        symbol=symbol,
        right=right,
        side=side,
        headline=f"LOG ONLY {symbol} {word}",
        detail=(
            f"{symbol} holding {'above' if side == 'call' else 'below'} ORB15 and VWAP. "
            f"Ask ${ask:.2f}. Confirms: {', '.join(tags)}. "
            f"Size ${spent:.0f} ({contracts} contract{'s' if contracts != 1 else ''})."
        ),
        confirms=len(tags),
        confirm_tags=tags,
        contract=candidate.get("contract") or None,
        strike=strike,
        expiry=candidate.get("expiry"),
        ask=ask,
        bid=bid,
        dte=dte,
        size_usd=spent,
        contracts=contracts,
        log_only=True,
        routed=False,
        exit_plan=_exit_plan(rules),
        orb_high=orb_high,
        orb_low=orb_low,
        vwap=vwap,
        last=last,
    )


def build_lottery_v2_board(
    candidates: list[dict[str, Any]] | None,
    *,
    tape_by_symbol: dict[str, dict[str, Any]] | None = None,
    rules: LotteryV2Rules | None = None,
    now: datetime | None = None,
    focus_names: list[str] | None = None,
    vix: float | None = None,
    vix_rising: bool | None = None,
    gex: float | None = None,
    gex_regime: str | None = None,
    flow_by_symbol: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rank LOG tickets and keep at most ``max_tickets`` for the day."""
    rules = rules or LotteryV2Rules()
    universe = lottery_v2_universe(focus_names)
    tape_by_symbol = tape_by_symbol or {}
    flow_by_symbol = {str(k).upper(): v for k, v in (flow_by_symbol or {}).items()}
    decided: list[LotteryV2Ticket] = []
    seen: set[str] = set()
    for raw in candidates or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("contract") or f"{raw.get('symbol')}|{raw.get('right')}|{raw.get('strike')}|{raw.get('expiry')}")
        if key in seen:
            continue
        seen.add(key)
        sym = str(raw.get("symbol") or "").upper()
        decided.append(
            decide_lottery_v2(
                raw,
                tape=tape_by_symbol.get(sym),
                rules=rules,
                now=now,
                universe=universe,
                vix=vix,
                vix_rising=vix_rising,
                gex=gex,
                gex_regime=gex_regime,
                flow_sentiment=flow_by_symbol.get(sym),
            )
        )

    logs = [t for t in decided if t.action == "LOG"]
    logs.sort(key=lambda t: (-t.confirms, float(t.ask or 99), t.symbol))
    kept = logs[: max(0, int(rules.max_tickets))]
    overflow = logs[max(0, int(rules.max_tickets)) :]
    for extra in overflow:
        extra.action = "SKIP"
        extra.vetoes = list(extra.vetoes) + ["max tickets for the day"]
        extra.detail = (extra.detail or "") + " Capped — max tickets for the day."
        extra.headline = extra.headline.replace("LOG ONLY", "SKIP")
    skips = [t for t in decided if t.action == "SKIP"]
    return {
        "desk": "lottery_v2",
        "title": "Lottery v2 — LOG ONLY",
        "log_only": True,
        "routed": False,
        "generated_at": _as_et(now).isoformat(),
        "rules": rules.to_dict(),
        "universe": sorted(universe),
        "tickets": [t.to_dict() for t in kept],
        "log": [t.to_dict() for t in kept],
        "skip": [t.to_dict() for t in skips[:12]],
        "counts": {
            "log": len(kept),
            "skip": len(skips),
            "scanned": len(decided),
        },
        "note": (
            "Log only. No Webull route and no paper-journal entry. "
            "A ticket needs a held ORB15 + VWAP break, a $0.05–$0.40 ask, 0–1 DTE, "
            "and at least one more confirm (volume, VIX regime, short-gamma, or matching flow)."
        ),
        "exit_plan": _exit_plan(rules),
    }


def _bar_time(bar: dict[str, Any]) -> datetime | None:
    raw = bar.get("ts") or bar.get("time") or bar.get("t")
    if isinstance(raw, datetime):
        return _as_et(raw)
    if raw is None:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        return _as_et(datetime.fromisoformat(text))
    except ValueError:
        return None


def _clock_reached(ts: datetime, hhmm: str) -> bool:
    return ts.timetz().replace(tzinfo=None) >= _hhmm(hhmm)


def replay_ladder(
    entry: float,
    bars: list[dict[str, Any]] | None,
    *,
    rules: LotteryV2Rules | None = None,
) -> dict[str, Any]:
    """Replay the log-only exit ladder on option prints after the fill.

    ``bars`` are chronological dicts with ``close`` and optional ``high`` / ``ts``.
    Half sells on the first close at or above 5×. The rest sells at 10×, on a
    50% giveback from the peak close after the 5× print, or at the hard time
    exit. If 5× never prints, both halves leave at the last close (the premium
    is the stop — a near-worthless close is a full loss).
    """
    rules = rules or LotteryV2Rules()
    entry = float(entry)
    series = [b for b in (bars or []) if _f(b.get("close")) is not None]
    if entry <= 0 or not series:
        return {
            "ok": False,
            "reason": "no entry or no bars",
            "hit_5x": False,
            "hit_10x": False,
            "went_to_zero": False,
            "blended_multiple": None,
            "ev_per_100": None,
        }

    half_px: float | None = None
    rest_px: float | None = None
    peak_close = entry
    peak_high = entry
    reason = "last_print"
    for bar in series:
        close = float(bar["close"])
        high = _f(bar.get("high"))
        if high is None:
            high = close
        peak_high = max(peak_high, high)
        prior_peak = peak_close
        peak_close = max(peak_close, close)
        ts = _bar_time(bar)
        if half_px is None and close >= entry * rules.half_multiple:
            half_px = close
        if half_px is not None and rest_px is None:
            if close >= entry * rules.rest_multiple:
                rest_px = close
                reason = "10x"
                break
            if prior_peak >= entry * rules.half_multiple and close <= prior_peak * (1.0 - rules.giveback_from_peak):
                rest_px = close
                reason = "giveback"
                break
        if ts is not None and _clock_reached(ts, rules.time_exit_et) and rest_px is None:
            if half_px is None:
                half_px = close
            rest_px = close
            reason = "time_exit"
            break

    last = float(series[-1]["close"])
    if half_px is None:
        half_px = last
        rest_px = last
        reason = "last_print"
    elif rest_px is None:
        rest_px = last
        reason = "last_print"
    blended = 0.5 * half_px + 0.5 * rest_px
    multiple = blended / entry
    hit_5x = any(float(b["close"]) >= entry * rules.half_multiple for b in series)
    hit_10x = any(float(b["close"]) >= entry * rules.rest_multiple for b in series)
    went_to_zero = (not hit_5x) and last <= 0.05
    return {
        "ok": True,
        "reason": reason,
        "entry": entry,
        "half_exit": round(half_px, 4),
        "rest_exit": round(rest_px, 4),
        "blended_exit": round(blended, 4),
        "blended_multiple": round(multiple, 4),
        "peak_close": round(peak_close, 4),
        "peak_high": round(peak_high, 4),
        "peak_multiple": round(peak_high / entry, 4),
        "last": last,
        "hit_5x": hit_5x,
        "hit_10x": hit_10x,
        "went_to_zero": went_to_zero,
        "pnl_per_contract": round((blended - entry) * 100.0, 2),
        "ev_per_100": round((multiple - 1.0) * 100.0, 2),
    }


def scan_session_break(
    bars: list[dict[str, Any]] | None,
    *,
    rules: LotteryV2Rules | None = None,
) -> dict[str, Any] | None:
    """First post-ORB close that holds beyond ORB15 and session VWAP.

    One signal per session. Bars need ``ts``, ``high``, ``low``, ``close``,
    and optional ``volume``. The opening range is 09:30–09:45 ET. Entries
    start once that window is complete and stop at ``entry_end_et``.
    """
    rules = rules or LotteryV2Rules()
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for bar in bars or []:
        ts = _bar_time(bar)
        close = _f(bar.get("close"))
        high = _f(bar.get("high"))
        low = _f(bar.get("low"))
        if ts is None or close is None or high is None or low is None:
            continue
        parsed.append((ts, bar))
    if not parsed:
        return None
    parsed.sort(key=lambda item: item[0])
    day = parsed[0][0].date()
    session = [(ts, bar) for ts, bar in parsed if ts.date() == day]
    orb_start = _hhmm("09:30")
    orb_end = _hhmm("09:45")
    entry_end = _hhmm(rules.entry_end_et)
    orb_bars = [bar for ts, bar in session if orb_start <= ts.timetz().replace(tzinfo=None) < orb_end]
    if not orb_bars:
        return None
    orb_high = max(float(b["high"]) for b in orb_bars)
    orb_low = min(float(b["low"]) for b in orb_bars)
    cum_pv = 0.0
    cum_v = 0.0
    volumes: list[float] = []
    for ts, bar in session:
        clock = ts.timetz().replace(tzinfo=None)
        if clock < orb_start:
            continue
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        vol = _f(bar.get("volume")) or 0.0
        typical = (high + low + close) / 3.0
        weight = vol if vol > 0 else 1.0
        cum_pv += typical * weight
        cum_v += weight
        vwap = cum_pv / cum_v if cum_v else None
        if vol > 0:
            volumes.append(vol)
        if clock < orb_end or clock >= entry_end or vwap is None:
            continue
        rel = None
        if len(volumes) >= 4 and volumes[-1] > 0:
            prior = volumes[:-1]
            med = sorted(prior)[len(prior) // 2]
            if med > 0:
                rel = volumes[-1] / med
        if close > orb_high and close > vwap:
            side = "call"
        elif close < orb_low and close < vwap:
            side = "put"
        else:
            continue
        return {
            "side": side,
            "right": "C" if side == "call" else "P",
            "ts": ts.isoformat(),
            "last": close,
            "orb_high": orb_high,
            "orb_low": orb_low,
            "vwap": vwap,
            "rel_volume": rel,
        }
    return None


def append_lottery_v2_log(board: dict[str, Any], path: str | Path) -> None:
    """Append today's LOG tickets. Never raises into the snapshot."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: list[Any] = []
    if target.exists():
        try:
            loaded = json.loads(target.read_text())
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = []
    seen = {
        (str(row.get("session_date")), str(row.get("contract") or row.get("symbol")))
        for row in existing
        if isinstance(row, dict)
    }
    day = str(board.get("generated_at") or "")[:10]
    for ticket in board.get("tickets") or []:
        if not isinstance(ticket, dict) or ticket.get("action") != "LOG":
            continue
        key = (day, str(ticket.get("contract") or ticket.get("symbol")))
        if key in seen:
            continue
        seen.add(key)
        existing.append({**ticket, "session_date": day, "logged_at": board.get("generated_at")})
    target.write_text(json.dumps(existing, indent=2, default=str))


def _row_contract(row: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return None
    ask = _f(row.get("ask") if row.get("ask") is not None else row.get("entry_ask"))
    if ask is None or ask <= 0:
        return None
    right = str(row.get("right") or "C").upper()[:1]
    return {
        "symbol": symbol,
        "right": "P" if right == "P" else "C",
        "contract": row.get("contract"),
        "strike": row.get("strike"),
        "expiry": row.get("expiry"),
        "dte": row.get("dte"),
        "ask": ask,
        "bid": row.get("bid"),
        "volume": row.get("volume"),
        "open_interest": row.get("open_interest") or row.get("oi"),
    }


def harvest_live_inputs(
    *,
    quotes: dict[str, Any] | None = None,
    explosive: list[dict[str, Any]] | None = None,
    radar: dict[str, Any] | None = None,
    chase: dict[str, Any] | None = None,
    rip: dict[str, Any] | None = None,
    odte_1k: dict[str, Any] | None = None,
    power_hour: dict[str, Any] | None = None,
    free_dealer: dict[str, Any] | None = None,
    flow_leaders: list[dict[str, Any]] | None = None,
    focus_names: list[str] | None = None,
) -> dict[str, Any]:
    """Pull contracts and tape already on the snapshot. Does not fetch."""
    universe = lottery_v2_universe(focus_names)
    candidates: list[dict[str, Any]] = []
    pools: list[Any] = list(explosive or [])
    for board, keys in (
        (radar or {}, ("hot", "watch", "tickets")),
        (chase or {}, ("buy_risky", "watch", "tickets")),
        (rip or {}, ("buy_rip", "watch", "cool", "all")),
        (odte_1k or {}, ("put_now", "watch", "all", "tickets")),
    ):
        for key in keys:
            pools.extend(board.get(key) or [])
    for row in pools:
        if not isinstance(row, dict):
            continue
        item = _row_contract(row)
        if item and item["symbol"] in universe:
            candidates.append(item)

    tape: dict[str, dict[str, Any]] = {}
    for sym, quote in (quotes or {}).items():
        if not isinstance(quote, dict):
            continue
        last = _f(quote.get("last"))
        if last is None:
            continue
        tape[str(sym).upper()] = {"last": last, "rel_volume": _f(quote.get("rel_volume"))}
    for row in (power_hour or {}).get("all") or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        slot = tape.setdefault(sym, {})
        if row.get("vwap") is not None:
            slot["vwap"] = row.get("vwap")
        if row.get("last") is not None:
            slot["last"] = row.get("last")
        if row.get("volume_expand") is True and slot.get("rel_volume") is None:
            slot["rel_volume"] = 1.25
    for key in ("put_now", "watch", "all", "tickets"):
        for row in (odte_1k or {}).get(key) or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            if not sym:
                continue
            slot = tape.setdefault(sym, {})
            if row.get("orb_high") is not None:
                slot["orb_high"] = row.get("orb_high")
            if row.get("orb_low") is not None:
                slot["orb_low"] = row.get("orb_low")
            if row.get("last") is not None and slot.get("last") is None:
                slot["last"] = row.get("last")

    vix = None
    vix_rising = None
    vol = (free_dealer or {}).get("vol_term") or {}
    levels = vol.get("levels") or {}
    vix = _f((levels.get("VIX") or {}).get("last"))
    vix1d = _f((levels.get("VIX1D") or {}).get("last"))
    if vix is not None and vix1d is not None:
        vix_rising = vix1d > vix + 0.3

    gex = None
    regime = None
    spx = (free_dealer or {}).get("spx_gex") or {}
    if spx.get("net_gex") is not None:
        gex = _f(spx.get("net_gex"))
        regime = spx.get("regime")
    sm = (free_dealer or {}).get("squeezemetrics") or {}
    if gex is None and sm.get("gex_billions") is not None:
        gex = _f(sm.get("gex_billions"))
        regime = sm.get("gex_regime")

    flow: dict[str, str] = {}
    for row in flow_leaders or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or row.get("ticker") or "").upper()
        sentiment = row.get("sentiment")
        if sym and sentiment and sym not in flow:
            flow[sym] = str(sentiment)

    return {
        "candidates": candidates[:48],
        "tape_by_symbol": tape,
        "vix": vix,
        "vix_rising": vix_rising,
        "gex": gex,
        "gex_regime": regime,
        "flow_by_symbol": flow,
    }
