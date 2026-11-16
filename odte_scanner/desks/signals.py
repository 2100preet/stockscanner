"""Pluggable entry signals for the Million Challenge desk.

Sources are named in ``million_challenge.signals``. Three ship with the desk:

* ``vwap_reclaim`` — the primary paper trade. SPY and QQQ only. On a closed
  1-minute or 5-minute bar during regular hours, price was below session
  VWAP and then trades at or above that VWAP times 1.005. The order is a
  near-ATM call expiring the same day, or the nearest expiry when that
  underlying has no 0DTE. New entries stop at about 2:30 PM CT. A 1DTE twin
  is attached for the desk to log without filling.
* ``challenge_quality`` — the scanner's quality-only Challenge entries
  (perfect/elite hist, snipers blocked), using ``ChallengeTracker`` as the
  definition of quality.
* ``oversold_bounce`` — SPY and QQQ only, log-only. A session that closes
  within 3% of the 40-day low, or at least 10% under the 25-day SMA, arms
  the next session. The entry is the first closed 1-minute or 5-minute bar
  that trades above that setup day's high, and the contract is a near-ATM
  call with the nearest expiry at least one day out. The desk records the
  ticket and does not fill it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.sp100 import in_oversold_universe, normalize_symbol
from odte_scanner.signals.hold_rules import entry_block_reason, trading_dte

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

CHALLENGE_ENTRY_ACTIONS = {"ENTRY", "BUY_NOW", "BUY_RIP"}
RTH_OPEN_CT = (8, 30)
RTH_CLOSE_CT = (15, 0)
VWAP_ENTRY_CUTOFF_CT = (14, 30)
VWAP_TOUCH = 1.005


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _as_ct(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(CT)
    if now.tzinfo is None:
        return now.replace(tzinfo=CT)
    return now.astimezone(CT)


def _closes(bars: list[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for bar in bars:
        close = _f(bar.get("close"))
        if close is not None and close > 0:
            out.append(close)
    return out


def _location(
    bars: list[Mapping[str, Any]],
    *,
    low_days: int,
    low_band: float,
    sma_days: int,
    sma_drop: float,
) -> list[str]:
    reasons: list[str] = []
    if len(bars) >= low_days:
        lows: list[float] = []
        for bar in bars[-low_days:]:
            low = _f(bar.get("low"))
            close = _f(bar.get("close"))
            price = low if low is not None and low > 0 else close
            if price is not None and price > 0:
                lows.append(price)
        last = _f(bars[-1].get("close"))
        if lows and last is not None:
            floor = min(lows)
            if floor > 0 and (last - floor) / floor <= low_band + 1e-12:
                reasons.append("near_40d_low")
    closes = _closes(bars)
    if len(closes) >= sma_days:
        average = sum(closes[-sma_days:]) / float(sma_days)
        if average > 0 and closes[-1] <= average * (1.0 - sma_drop) + 1e-9:
            reasons.append("below_sma25")
    return reasons


def _bar_date(bar: Mapping[str, Any]) -> date | None:
    raw = bar.get("date") or bar.get("ts") or bar.get("time")
    if isinstance(raw, datetime):
        return _as_ct(raw).date()
    if isinstance(raw, date):
        return raw
    text = str(raw or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _bar_time(bar: Mapping[str, Any]) -> datetime | None:
    raw = bar.get("ts") or bar.get("time") or bar.get("date")
    if isinstance(raw, datetime):
        return _as_ct(raw)
    text = str(raw or "")
    try:
        return _as_ct(datetime.fromisoformat(text))
    except ValueError:
        return None


def _next_session(day: date) -> date:
    cursor = day + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _hhmm(value: Any, default: tuple[int, int] = (14, 55)) -> tuple[int, int]:
    try:
        hour, minute = str(value or "").split(":")
        return int(hour), int(minute)
    except (TypeError, ValueError):
        return default


def select_bounce_call(
    chain: list[Mapping[str, Any]] | None,
    spot: float,
    now: datetime | None,
) -> dict[str, Any] | None:
    """Near-ATM call on the nearest expiry that is at least one day out.

    Same-day (0DTE) contracts are skipped. Among the soonest remaining
    expiry, the strike closest to spot wins.
    """
    if not chain or spot <= 0:
        return None
    best: tuple[tuple[int, float, int, float], dict[str, Any]] | None = None
    for row in chain:
        if not isinstance(row, Mapping):
            continue
        right = str(row.get("right") or "C").upper()
        if right == "CALL":
            right = "C"
        if right != "C":
            continue
        dte = row.get("dte")
        if dte is None and row.get("expiry"):
            dte = trading_dte(str(row.get("expiry")), now)
        try:
            dte_i = int(dte)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if dte_i < 1:
            continue
        strike = _f(row.get("strike"))
        ask = _f(row.get("ask"))
        if strike is None or strike <= 0 or ask is None or ask <= 0:
            continue
        bid = _f(row.get("bid")) or 0.0
        dist = abs(strike - spot)
        otm_bias = 0 if strike >= spot else 1
        spread = abs(ask - bid)
        key = (dte_i, round(dist, 6), otm_bias, spread)
        picked = dict(row)
        picked["dte"] = dte_i
        picked["right"] = "C"
        if best is None or key < best[0]:
            best = (key, picked)
    return None if best is None else best[1]


def _intraday_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get("bars")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, Mapping)]
    return []


def _first_break(
    payload: Any,
    *,
    setup_high: float,
    today: date,
    now: datetime,
    flatten_hm: tuple[int, int],
) -> Mapping[str, Any] | None:
    """First closed 1-minute or 5-minute bar that trades above ``setup_high``.

    A daily bar, a still-forming bar, and any bar at or after the flatten
    clock are ignored. Entering on the session close is not this trigger.
    """
    found: list[tuple[datetime, Mapping[str, Any]]] = []
    for bar in _intraday_rows(payload):
        if bar.get("closed") is False:
            continue
        interval = str(bar.get("interval") or "1m").lower().replace(" ", "")
        if interval in {"1d", "1day", "daily", "d"}:
            continue
        if interval not in {"1m", "5m", "1min", "5min"}:
            continue
        stamp = _bar_time(bar)
        if stamp is None or stamp.date() != today or stamp > now:
            continue
        if (stamp.hour, stamp.minute) >= flatten_hm:
            continue
        high = _f(bar.get("high"))
        if high is None:
            high = _f(bar.get("close"))
        if high is None or high <= setup_high:
            continue
        found.append((stamp, bar))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def challenge_quality_signals(
    board: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Quality-only Challenge entries. Snipers and weak hist stay off."""
    if not isinstance(board, dict):
        return []
    from odte_scanner.challenge.tracker import ChallengeTracker

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def consider(ticket: Mapping[str, Any], *, from_entry: bool) -> None:
        if not isinstance(ticket, Mapping):
            return
        action = str(ticket.get("action") or ("ENTRY" if from_entry else "")).upper()
        if action in {"EXIT", "WAIT", "HOLD", "SELL", "SELL_NOW"}:
            return
        if action and action not in CHALLENGE_ENTRY_ACTIONS:
            return
        if not action and not from_entry:
            return
        if not ChallengeTracker.ticket_is_quality(dict(ticket)):
            return
        if ChallengeTracker.ticket_is_sniper(dict(ticket)):
            return
        ask = _f(ticket.get("ask"))
        if ask is None or ask <= 0:
            return
        symbol = normalize_symbol(str(ticket.get("symbol") or ""))
        if not symbol:
            return
        expiry = str(ticket.get("expiry") or "")[:10]
        block = entry_block_reason(expiry or None, now, dte=_maybe_int(ticket.get("dte")))
        if block:
            return
        contract = str(ticket.get("contract") or "")
        key = (symbol, contract, str(ticket.get("right") or "C").upper())
        if key in seen:
            return
        seen.add(key)
        right = str(ticket.get("right") or "C").upper()
        if right == "CALL":
            right = "C"
        rows.append(
            {
                "source": "challenge_quality",
                "symbol": symbol,
                "right": right if right in {"C", "P"} else "C",
                "contract": contract or None,
                "expiry": expiry or None,
                "strike": _f(ticket.get("strike")),
                "dte": _maybe_int(ticket.get("dte")),
                "bid": _f(ticket.get("bid")),
                "ask": ask,
                "spot": _f(ticket.get("spot")),
                "reason": "challenge_quality",
            }
        )

    for ticket in board.get("entry") or []:
        consider(ticket, from_entry=True)
    for ticket in board.get("tickets") or []:
        consider(ticket, from_entry=False)
    return rows


def oversold_bounce_signals(
    bars_by_symbol: Mapping[str, list[Mapping[str, Any]]] | None,
    *,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    settings: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SPY/QQQ setup from the prior close, triggered on the next session.

    The setup is the last completed daily bar: close within 3% of the
    40-session low, or at least 10% under the 25-day SMA. The entry is the
    first closed 1-minute or 5-minute bar of the following session whose
    high trades above that day's high. There is no entry on the setup close
    itself, and no 0DTE call.
    """
    cfg = dict(settings or {})
    low_days = int(cfg.get("low_days") or 40)
    low_band = float(cfg.get("low_band_pct") or 0.03)
    sma_days = int(cfg.get("sma_days") or 25)
    sma_drop = float(cfg.get("sma_drop_pct") or 0.10)
    flatten_hm = _hhmm(cfg.get("flatten_ct") or "14:55")
    moment = _as_ct(now)
    today = moment.date()
    if moment.weekday() >= 5 or (moment.hour, moment.minute) < (8, 30):
        return []
    if (moment.hour, moment.minute) >= flatten_hm:
        return []
    found: list[dict[str, Any]] = []
    for raw_symbol, bars in (bars_by_symbol or {}).items():
        symbol = normalize_symbol(str(raw_symbol))
        if not in_oversold_universe(symbol) or not bars:
            continue
        prior = [bar for bar in bars if (stamp := _bar_date(bar)) is not None and stamp < today]
        prior.sort(key=lambda bar: _bar_date(bar) or date.min)
        if not prior:
            continue
        setup = prior[-1]
        setup_day = _bar_date(setup)
        if setup_day is None or _next_session(setup_day) != today:
            continue
        where = _location(
            prior,
            low_days=low_days,
            low_band=low_band,
            sma_days=sma_days,
            sma_drop=sma_drop,
        )
        if not where:
            continue
        setup_high = _f(setup.get("high"))
        if setup_high is None:
            setup_high = _f(setup.get("close"))
        if setup_high is None:
            continue
        tape = None if intraday is None else intraday.get(symbol)
        trigger = _first_break(tape, setup_high=setup_high, today=today, now=moment, flatten_hm=flatten_hm)
        if trigger is None:
            continue
        spot = _f(trigger.get("close")) or _f(setup.get("close")) or 0.0
        chain = None if chains is None else chains.get(symbol)
        contract = select_bounce_call(list(chain) if chain else None, spot, moment)
        if contract is None:
            continue
        expiry = str(contract.get("expiry") or "")[:10] or None
        found.append(
            {
                "source": "oversold_bounce",
                "log_only": True,
                "symbol": symbol,
                "right": "C",
                "contract": contract.get("contract"),
                "expiry": expiry,
                "strike": _f(contract.get("strike")),
                "dte": contract.get("dte"),
                "bid": _f(contract.get("bid")),
                "ask": _f(contract.get("ask")),
                "spot": spot,
                "setup_high": setup_high,
                "reason": "+".join([*where, "break_setup_high"]),
            }
        )
    found.sort(key=lambda row: row["symbol"])
    return found


def _clock(moment: datetime) -> tuple[int, int, int]:
    ct = _as_ct(moment)
    return ct.hour, ct.minute, ct.second


def _before(moment: datetime, hm: tuple[int, int]) -> bool:
    """True when ``moment`` is strictly before ``hm`` on the CT clock.

    The comparison uses the CT instant, so a UTC timestamp is not read as
    if its wall-clock hour were Chicago time.
    """
    return _clock(moment) < (hm[0], hm[1], 0)


def _in_rth(moment: datetime) -> bool:
    return _clock(moment) >= (RTH_OPEN_CT[0], RTH_OPEN_CT[1], 0) and _before(moment, RTH_CLOSE_CT)


def _norm_interval(value: Any) -> str | None:
    text = str(value or "1m").lower().replace(" ", "")
    if text in {"1m", "1min"}:
        return "1m"
    if text in {"5m", "5min"}:
        return "5m"
    return None


def _bar_volume(bar: Mapping[str, Any]) -> float:
    for key in ("volume", "Volume", "vol"):
        number = _f(bar.get(key))
        if number is not None and number > 0:
            return number
    return 1.0


def _running_vwap(bars: list[Mapping[str, Any]]) -> list[float]:
    """Session VWAP from typical price. Missing volume counts as one share."""
    total = 0.0
    volume = 0.0
    out: list[float] = []
    for bar in bars:
        close = _f(bar.get("close"))
        high = _f(bar.get("high"))
        low = _f(bar.get("low"))
        if close is None or close <= 0:
            out.append(total / volume if volume else 0.0)
            continue
        if high is None or high <= 0:
            high = close
        if low is None or low <= 0:
            low = close
        typical = (high + low + close) / 3.0
        vol = _bar_volume(bar)
        total += typical * vol
        volume += vol
        out.append(total / volume if volume else typical)
    return out


def select_reclaim_call(
    chain: list[Mapping[str, Any]] | None,
    spot: float,
    now: datetime | None,
    *,
    min_dte: int = 0,
    prefer_dte: int | None = 0,
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
    """Near-ATM call. Prefer ``prefer_dte`` (0 is same-day), else the nearest.

    ``min_dte`` drops nearer expiries. ``exclude`` drops contracts already
    used, so the 1DTE twin is not the same contract as the fill.
    """
    if not chain or spot <= 0:
        return None
    skipped = exclude or set()
    best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    for row in chain:
        if not isinstance(row, Mapping):
            continue
        right = str(row.get("right") or "C").upper()
        if right == "CALL":
            right = "C"
        if right != "C":
            continue
        contract_id = str(row.get("contract") or "")
        if contract_id and contract_id in skipped:
            continue
        dte = row.get("dte")
        if dte is None and row.get("expiry"):
            dte = trading_dte(str(row.get("expiry")), now)
        try:
            dte_i = int(dte)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if dte_i < min_dte:
            continue
        strike = _f(row.get("strike"))
        ask = _f(row.get("ask"))
        if strike is None or strike <= 0 or ask is None or ask <= 0:
            continue
        bid = _f(row.get("bid")) or 0.0
        prefer_rank = 0 if prefer_dte is not None and dte_i == prefer_dte else 1
        dist = abs(strike - spot)
        otm_bias = 0 if strike >= spot else 1
        spread = abs(ask - bid)
        key = (prefer_rank, dte_i, round(dist, 6), otm_bias, spread)
        picked = dict(row)
        picked["dte"] = dte_i
        picked["right"] = "C"
        if best is None or key < best[0]:
            best = (key, picked)
    return None if best is None else best[1]


def _series_bars(
    payload: Any,
    *,
    interval: str,
    today: date,
    now: datetime,
) -> list[tuple[datetime, Mapping[str, Any]]]:
    found: list[tuple[datetime, Mapping[str, Any]]] = []
    for bar in _intraday_rows(payload):
        if bar.get("closed") is False:
            continue
        if _norm_interval(bar.get("interval")) != interval:
            continue
        stamp = _bar_time(bar)
        if stamp is None or stamp.date() != today or stamp > now:
            continue
        if not _in_rth(stamp):
            continue
        found.append((stamp, bar))
    found.sort(key=lambda item: item[0])
    return found


def _first_reclaim(
    payload: Any,
    *,
    today: date,
    now: datetime,
    touch: float,
    cutoff: tuple[int, int],
) -> tuple[datetime, Mapping[str, Any], float, str] | None:
    """Earliest closed 1m or 5m bar that reclaims session VWAP by ``touch``.

    VWAP is computed per interval so 1-minute and 5-minute bars are not
    added together. Price must have printed below that series' VWAP first.
    Bars at or after the entry cutoff cannot open a new trade.
    """
    found: tuple[datetime, Mapping[str, Any], float, str] | None = None
    for interval in ("1m", "5m"):
        series = _series_bars(payload, interval=interval, today=today, now=now)
        if not series:
            continue
        vwaps = _running_vwap([bar for _stamp, bar in series])
        was_below = False
        for (stamp, bar), vwap in zip(series, vwaps):
            if vwap <= 0 or not _before(stamp, cutoff):
                continue
            close = _f(bar.get("close"))
            high = _f(bar.get("high"))
            low = _f(bar.get("low"))
            if high is None:
                high = close
            if low is None:
                low = close
            if low is not None and low < vwap - 1e-9:
                was_below = True
            level = vwap * touch
            if was_below and high is not None and high + 1e-9 >= level:
                if found is None or stamp < found[0] or (stamp == found[0] and interval == "1m"):
                    found = (stamp, bar, vwap, interval)
                break
    return found


def _reclaim_idea(
    *,
    source: str,
    symbol: str,
    contract: Mapping[str, Any],
    spot: float,
    log_only: bool,
    reason: str,
    target_pct: float,
    stop_pct: float,
    time_stop_minutes: float | None,
    flatten_ct: str | None,
    vwap: float,
    interval: str,
) -> dict[str, Any]:
    expiry = str(contract.get("expiry") or "")[:10] or None
    return {
        "source": source,
        "log_only": log_only,
        "symbol": symbol,
        "right": "C",
        "contract": contract.get("contract"),
        "expiry": expiry,
        "strike": _f(contract.get("strike")),
        "dte": contract.get("dte"),
        "bid": _f(contract.get("bid")),
        "ask": _f(contract.get("ask")),
        "spot": spot,
        "vwap": vwap,
        "interval": interval,
        "reason": reason,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "time_stop_minutes": time_stop_minutes,
        "flatten_ct": flatten_ct,
    }


def vwap_reclaim_signals(
    bars_by_symbol: Mapping[str, list[Mapping[str, Any]]] | None = None,
    *,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    settings: Mapping[str, Any] | None = None,
    twin_settings: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SPY/QQQ VWAP reclaim. The idea is the paper trade; ``twin`` is log-only.

    During regular hours a closed 1-minute or 5-minute bar must trade at or
    above session VWAP × 1.005 after price has already printed below that
    VWAP. Nothing new is signaled at or after 2:30 PM CT. The call expires
    today when a 0DTE contract exists, otherwise on the nearest expiry.
    """
    del bars_by_symbol  # daily bars do not define this entry
    cfg = dict(settings or {})
    twin_cfg = dict(twin_settings or {})
    try:
        touch = 1.0 + float(cfg.get("touch_pct") if cfg.get("touch_pct") is not None else 0.005)
    except (TypeError, ValueError):
        touch = VWAP_TOUCH
    if touch <= 1.0:
        touch = VWAP_TOUCH
    cutoff = _hhmm(cfg.get("entry_cutoff_ct") or "14:30", VWAP_ENTRY_CUTOFF_CT)
    target = float(cfg.get("target_pct") or 50)
    stop = float(cfg.get("stop_pct") or 40)
    try:
        time_stop = float(cfg.get("time_stop_minutes") if cfg.get("time_stop_minutes") is not None else 60)
    except (TypeError, ValueError):
        time_stop = 60.0
    flatten = str(cfg.get("flatten_ct") or "14:55")
    twin_target = float(twin_cfg.get("target_pct") or 50)
    twin_stop = float(twin_cfg.get("stop_pct") or 30)
    try:
        twin_time = float(twin_cfg.get("time_stop_minutes") if twin_cfg.get("time_stop_minutes") is not None else 60)
    except (TypeError, ValueError):
        twin_time = 60.0
    moment = _as_ct(now)
    today = moment.date()
    if moment.weekday() >= 5 or not _in_rth(moment) or not _before(moment, cutoff):
        return []
    found: list[dict[str, Any]] = []
    for raw_symbol, tape in (intraday or {}).items():
        symbol = normalize_symbol(str(raw_symbol))
        if not in_oversold_universe(symbol):
            continue
        reclaim = _first_reclaim(tape, today=today, now=moment, touch=touch, cutoff=cutoff)
        if reclaim is None:
            continue
        _stamp, bar, vwap, interval = reclaim
        spot = _f(bar.get("close")) or 0.0
        if spot <= 0:
            continue
        chain = None if chains is None else chains.get(symbol)
        primary = select_reclaim_call(list(chain) if chain else None, spot, moment, min_dte=0, prefer_dte=0)
        if primary is None:
            continue
        idea = _reclaim_idea(
            source="vwap_reclaim",
            symbol=symbol,
            contract=primary,
            spot=spot,
            log_only=False,
            reason="vwap_reclaim",
            target_pct=target,
            stop_pct=stop,
            time_stop_minutes=time_stop,
            flatten_ct=flatten,
            vwap=vwap,
            interval=interval,
        )
        primary_id = str(primary.get("contract") or "")
        twin = select_reclaim_call(
            list(chain) if chain else None,
            spot,
            moment,
            min_dte=1,
            prefer_dte=1,
            exclude={primary_id} if primary_id else set(),
        )
        if twin is not None:
            idea["twin"] = _reclaim_idea(
                source="vwap_reclaim_1dte",
                symbol=symbol,
                contract=twin,
                spot=spot,
                log_only=True,
                reason="vwap_reclaim_1dte",
                target_pct=twin_target,
                stop_pct=twin_stop,
                time_stop_minutes=twin_time,
                flatten_ct=None,
                vwap=vwap,
                interval=interval,
            )
        else:
            idea["twin"] = None
        found.append(idea)
    found.sort(key=lambda row: (row["symbol"], row.get("interval") or ""))
    return found


def intraday_bars_from_frame(frame: Any, *, interval: str = "1m", now: datetime | None = None) -> list[dict[str, Any]]:
    """Turn an OHLCV frame into closed-bar dicts without dropping the timezone.

    A naive index is the exchange clock (US/Eastern), not Chicago and not
    UTC. A timezone-aware index keeps its instant. The last bar stays open
    until its end is at or before ``now``.
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    moment = _as_ct(now)
    minutes = 5 if str(interval).startswith("5") else 1
    rows: list[dict[str, Any]] = []
    for stamp, row in frame.iterrows():
        if isinstance(stamp, datetime):
            bar_time = stamp
        else:
            to_py = getattr(stamp, "to_pydatetime", None)
            if to_py is None:
                continue
            bar_time = to_py()
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=ET)
        end = bar_time + timedelta(minutes=minutes)

        def col(*names: str) -> Any:
            for name in names:
                if name in row:
                    return row[name]
            return None

        close = _f(col("Close", "close"))
        if close is None or close <= 0:
            continue
        high = _f(col("High", "high")) or close
        low = _f(col("Low", "low")) or close
        opened = _f(col("Open", "open")) or close
        volume = _f(col("Volume", "volume"))
        rows.append(
            {
                "ts": bar_time,
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume if volume is not None else 0.0,
                "interval": "5m" if minutes == 5 else "1m",
                "closed": end <= moment,
            }
        )
    return rows


def build_signals(
    names: list[str] | tuple[str, ...] | None,
    *,
    challenge: Mapping[str, Any] | None = None,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    oversold_settings: Mapping[str, Any] | None = None,
    vwap_settings: Mapping[str, Any] | None = None,
    twin_settings: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the configured sources in order. Unknown names are ignored.

    ``vwap_reclaim`` is the primary trade. ``oversold_bounce`` is returned
    with ``log_only`` set, and its 1DTE twin rides on the reclaim idea.
    """
    out: list[dict[str, Any]] = []
    ignored: list[str] = []
    for name in names or []:
        key = str(name or "").strip()
        if key == "vwap_reclaim":
            out.extend(
                vwap_reclaim_signals(
                    bars,
                    chains=chains,
                    intraday=intraday,
                    now=now,
                    settings=vwap_settings,
                    twin_settings=twin_settings,
                )
            )
        elif key == "challenge_quality":
            out.extend(challenge_quality_signals(challenge, now=now))
        elif key == "oversold_bounce":
            out.extend(
                oversold_bounce_signals(
                    bars,
                    chains=chains,
                    intraday=intraday,
                    now=now,
                    settings=oversold_settings,
                )
            )
        elif key:
            ignored.append(key)
    return out, ignored


def _maybe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
