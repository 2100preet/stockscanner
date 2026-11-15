"""Pluggable entry signals for the Million Challenge desk.

Sources are named in ``million_challenge.signals``. Two ship with the desk:

* ``challenge_quality`` — the scanner's quality-only Challenge entries
  (perfect/elite hist, snipers blocked), using ``ChallengeTracker`` as the
  definition of quality.
* ``oversold_bounce`` — SPY and QQQ only. A session that closes within 3% of
  the 40-day low, or at least 10% under the 25-day SMA, arms the next
  session. The entry is the first closed 1-minute or 5-minute bar that
  trades above that setup day's high, and the order is a near-ATM call with
  the nearest expiry at least one day out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.sp100 import in_oversold_universe, normalize_symbol
from odte_scanner.signals.hold_rules import entry_block_reason, trading_dte

CT = ZoneInfo("America/Chicago")

CHALLENGE_ENTRY_ACTIONS = {"ENTRY", "BUY_NOW", "BUY_RIP"}


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


def build_signals(
    names: list[str] | tuple[str, ...] | None,
    *,
    challenge: Mapping[str, Any] | None = None,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    oversold_settings: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run the configured sources in order. Unknown names are ignored."""
    out: list[dict[str, Any]] = []
    ignored: list[str] = []
    for name in names or []:
        key = str(name or "").strip()
        if key == "challenge_quality":
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
