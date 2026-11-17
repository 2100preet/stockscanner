"""Kotegawa paper desk. Stocks, daily bars, its own ledger.

Entry near the cash close (14:50–15:00 America/Chicago): buy a current S&P 100
name whose close is at least 10% below its 25-day SMA. No SPY filter. Rank by
the deepest deviation. At most five open positions, each 20% of this desk's
equity. Starting equity is $100,000 paper.

Exits: +5% take-profit on an intraday check, a −15% disaster stop, or the
close of trading day 3 (the entry session counts as day 1). There is no
−2%/−3% stop. New entries halt after −$3,000 realized on this desk today.
Exits still run.

State file, cash, and the daily realized total belong to this desk only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.fills import buy_fill, sell_fill
from odte_scanner.desks.routing import resolve_route, submit_sandbox_order
from odte_scanner.desks.sp100 import in_sp100, normalize_symbol
from odte_scanner.desks.store import DeskStateError, load_desk_state, save_desk_state

CT = ZoneInfo("America/Chicago")
DESK = "kotegawa"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / "outputs" / "kotegawa_state.json"

STARTING_EQUITY = 100_000.0
MAX_POSITIONS = 5
SLOT_PCT = 0.20
SMA_DAYS = 25
ENTRY_DROP = 0.10
TAKE_PROFIT = 0.05
DISASTER_STOP = 0.15
HOLD_SESSIONS = 3
DAILY_LOSS_USD = 3_000.0


def _as_ct(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(CT)
    if now.tzinfo is None:
        return now.replace(tzinfo=CT)
    return now.astimezone(CT)


def _session_date(now: datetime | None) -> date:
    return _as_ct(now).date()


def _in_entry_window(now: datetime | None) -> bool:
    ct = _as_ct(now)
    if ct.weekday() >= 5:
        return False
    minutes = ct.hour * 60 + ct.minute
    return (14 * 60 + 50) <= minutes <= (15 * 60)


def _at_or_after_close(now: datetime | None) -> bool:
    ct = _as_ct(now)
    return (ct.hour, ct.minute) >= (15, 0)


def _sessions_inclusive(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += timedelta(days=1)
    return count


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _closes(bars: list[Mapping[str, Any]]) -> list[float]:
    out: list[float] = []
    for bar in bars:
        close = _f(bar.get("close"))
        if close is not None and close > 0:
            out.append(close)
    return out


def sma(closes: list[float], window: int = SMA_DAYS) -> float | None:
    if len(closes) < window or window <= 0:
        return None
    chunk = closes[-window:]
    return sum(chunk) / float(window)


def deviation(close: float, average: float) -> float:
    if average <= 0:
        return 0.0
    return (close / average) - 1.0


def _bar_on(bars: list[Mapping[str, Any]] | None, session: date) -> Mapping[str, Any] | None:
    if not bars:
        return None
    key = session.isoformat()
    for bar in reversed(bars):
        if str(bar.get("date") or "")[:10] == key:
            return bar
    return None


def _quote(quotes: Mapping[str, Any] | None, symbol: str) -> Mapping[str, Any]:
    if not isinstance(quotes, Mapping):
        return {}
    raw = quotes.get(symbol)
    return raw if isinstance(raw, Mapping) else {}


def candidates(
    bars_by_symbol: Mapping[str, list[Mapping[str, Any]]] | None,
    *,
    sma_days: int = SMA_DAYS,
    entry_drop: float = ENTRY_DROP,
) -> list[dict[str, Any]]:
    """S&P 100 names at least ``entry_drop`` below the SMA, deepest first.

    Symbols outside the S&P 100 are ignored. SPY is not a gate and is not
    in this universe.
    """
    found: list[dict[str, Any]] = []
    for raw_symbol, bars in (bars_by_symbol or {}).items():
        symbol = normalize_symbol(str(raw_symbol))
        if not in_sp100(symbol) or not bars:
            continue
        closes = _closes(list(bars))
        average = sma(closes, sma_days)
        if average is None:
            continue
        close = closes[-1]
        gap = deviation(close, average)
        if gap > -abs(entry_drop) + 1e-12:
            continue
        found.append(
            {
                "symbol": symbol,
                "close": close,
                "sma": average,
                "deviation": gap,
                "bar": dict(bars[-1]),
            }
        )
    found.sort(key=lambda row: (row["deviation"], row["symbol"]))
    return found


def _fresh(starting: float) -> dict[str, Any]:
    return {
        "desk": DESK,
        "paper_only": True,
        "starting_equity": starting,
        "cash": starting,
        "trades": [],
        "recent_alerts": [],
        "sandbox_orders": [],
    }


def _load(path: Path, starting: float) -> dict[str, Any]:
    saved = load_desk_state(path, DESK)
    if not isinstance(saved, dict):
        return _fresh(starting)
    book = _fresh(starting)
    book["starting_equity"] = float(saved.get("starting_equity") or starting)
    book["cash"] = float(saved.get("cash") if saved.get("cash") is not None else book["starting_equity"])
    book["trades"] = [dict(row) for row in saved.get("trades") or [] if isinstance(row, dict)]
    book["recent_alerts"] = list(saved.get("recent_alerts") or [])[-30:]
    book["sandbox_orders"] = list(saved.get("sandbox_orders") or [])[-30:]
    return book


def _open_trades(book: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in book["trades"] if row.get("status") == "open"]


def _mark_for(trade: Mapping[str, Any], quotes: Mapping[str, Any] | None, bars: list[Mapping[str, Any]] | None) -> float:
    symbol = str(trade.get("symbol") or "")
    quote = _quote(quotes, symbol)
    for key in ("last", "bid", "close"):
        price = _f(quote.get(key))
        if price is not None and price > 0:
            return price
    if bars:
        close = _f(bars[-1].get("close"))
        if close is not None and close > 0:
            return close
    return float(trade.get("entry") or 0)


def equity_of(book: Mapping[str, Any], marks: Mapping[str, float] | None = None) -> float:
    total = float(book.get("cash") or 0)
    for trade in book.get("trades") or []:
        if not isinstance(trade, dict) or trade.get("status") != "open":
            continue
        symbol = str(trade.get("symbol") or "")
        mark = None if marks is None else marks.get(symbol)
        if mark is None:
            mark = float(trade.get("entry") or 0)
        total += float(trade.get("shares") or 0) * float(mark)
    return total


def _realized_on(book: Mapping[str, Any], session: date) -> float:
    key = session.isoformat()
    total = 0.0
    for trade in book.get("trades") or []:
        if not isinstance(trade, dict) or trade.get("status") != "closed":
            continue
        if str(trade.get("exit_session") or "") != key:
            continue
        total += float(trade.get("pnl") or 0)
    return total


def _prices(
    symbol: str,
    bars: list[Mapping[str, Any]] | None,
    quotes: Mapping[str, Any] | None,
    session: date,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return high, low, last, bid for this session."""
    quote = _quote(quotes, symbol)
    bar = _bar_on(bars, session)
    last = _f(quote.get("last"))
    bid = _f(quote.get("bid"))
    high = _f(bar.get("high")) if bar else None
    low = _f(bar.get("low")) if bar else None
    close = _f(bar.get("close")) if bar else None
    if last is None:
        last = _f(quote.get("close")) or close
    if high is None:
        high = last
    elif last is not None:
        high = max(high, last)
    if low is None:
        low = last
    elif last is not None:
        low = min(low, last)
    if bid is not None and low is not None:
        low = min(low, bid)
    elif bid is not None:
        low = bid
    if bid is None:
        bid = last if last is not None else close
    return high, low, last, bid


def _exit_reason(
    trade: Mapping[str, Any],
    *,
    high: float | None,
    low: float | None,
    now: datetime,
    hold_sessions: int,
) -> str | None:
    entry = float(trade.get("entry") or 0)
    if entry <= 0:
        return None
    # Disaster stop is the only loss exit. A −2% or −3% print is not a stop.
    if low is not None and low <= entry * (1.0 - DISASTER_STOP):
        return "disaster_stop"
    if high is not None and high >= entry * (1.0 + TAKE_PROFIT):
        return "take_profit"
    try:
        started = date.fromisoformat(str(trade.get("entry_session")))
    except ValueError:
        return None
    held = _sessions_inclusive(started, _session_date(now))
    if held > hold_sessions:
        return "time_exit"
    if held == hold_sessions and _at_or_after_close(now):
        return "time_exit"
    return None


def _alert_session(side: str, trade: Mapping[str, Any]) -> str | None:
    raw = trade.get("exit_session") if side == "SELL" else trade.get("entry_session")
    text = str(raw or "")[:10]
    return text or None


def _alert(side: str, trade: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Fill notice for the portal and Telegram. This does not place an order."""
    return {
        "key": f"KOTEGAWA|{side}|{trade.get('id')}",
        "side": side,
        "desk": "Kotegawa",
        "symbol": trade.get("symbol"),
        "action": side,
        "message": text,
        "session": _alert_session(side, trade),
        "paper": True,
        "log_only": False,
        "shares": trade.get("shares"),
        "entry": trade.get("entry"),
        "exit": trade.get("exit"),
        "ask": trade.get("entry") if side == "BUY" else None,
        "bid": trade.get("exit") if side == "SELL" else None,
        "row": {
            "symbol": trade.get("symbol"),
            "id": trade.get("id"),
            "shares": trade.get("shares"),
            "entry": trade.get("entry"),
        },
    }


def _remember(book: dict[str, Any], alert: dict[str, Any]) -> None:
    book["recent_alerts"] = (list(book.get("recent_alerts") or []) + [alert])[-30:]


def _open_position(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = _f(row.get("entry"))
    mark = _f(row.get("last_mark"))
    shares = int(row.get("shares") or 0)
    unrealized = None
    if entry is not None and mark is not None and shares > 0:
        unrealized = round((mark - entry) * shares, 2)
    symbol = row.get("symbol")
    contract = f"{symbol} {shares} sh" if symbol and shares else symbol
    return {
        "symbol": symbol,
        "shares": row.get("shares"),
        "contract": contract,
        "entry": row.get("entry"),
        "entry_session": row.get("entry_session"),
        "mark": mark,
        "unrealized_pnl": unrealized,
    }


def public_view(
    book: Mapping[str, Any],
    *,
    status: str = "ok",
    reason: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    trades = [row for row in book.get("trades") or [] if isinstance(row, dict)]
    closed = [row for row in trades if row.get("status") == "closed"]
    opened = [row for row in trades if row.get("status") == "open"]
    wins = sum(1 for row in closed if float(row.get("pnl") or 0) > 0)
    losses = sum(1 for row in closed if float(row.get("pnl") or 0) < 0)
    realized = sum(float(row.get("pnl") or 0) for row in closed)
    # Equity stays marked at entry so the scorecard number does not move
    # when a quote is missing. The open row carries the live mark separately.
    marks = {str(row.get("symbol")): float(row.get("entry") or 0) for row in opened}
    equity = equity_of(book, marks)
    win_rate = (wins / len(closed)) if closed else None
    day = as_of or _as_ct(None).date()
    return {
        "desk": DESK,
        "name": "Kotegawa",
        "enabled": status != "disabled",
        "paper_only": True,
        "status": status,
        "reason": reason,
        "route": book.get("route") or "internal",
        "equity": round(equity, 2),
        "cash": round(float(book.get("cash") or 0), 2),
        "starting_equity": float(book.get("starting_equity") or STARTING_EQUITY),
        "realized_pnl": round(realized, 2),
        "realized_today": round(_realized_on(book, day), 2),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "trades": len(closed),
        "open_positions": len(opened),
        "open": [_open_position(row) for row in opened],
        "entries_halted": bool(book.get("entries_halted")),
        "halt_reason": book.get("halt_reason"),
        "alerts": list(book.get("recent_alerts") or []),
        "state_path": book.get("state_path"),
    }


def scorecard_row(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "desk": "Kotegawa",
        "trades": int(view.get("trades") or 0),
        "win_rate": view.get("win_rate"),
        "realized_pnl": view.get("realized_pnl"),
        "open_positions": int(view.get("open_positions") or 0),
        "equity": view.get("equity"),
    }


def run_kotegawa(
    section: Mapping[str, Any] | None,
    *,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    quotes: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    path: str | Path | None = None,
    host: str | None = None,
    live_trading: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """Advance the Kotegawa book. Paper only. Returns the public snapshot."""
    cfg = dict(section or {})
    starting = float(cfg.get("starting_equity") or STARTING_EQUITY)
    state_path = Path(path or cfg.get("state_path") or DEFAULT_STATE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    hold_sessions = int(cfg.get("hold_sessions") or HOLD_SESSIONS)
    max_positions = int(cfg.get("max_positions") or MAX_POSITIONS)
    slot_pct = float(cfg.get("slot_pct") or SLOT_PCT)
    daily_loss = float(cfg.get("daily_loss_usd") or DAILY_LOSS_USD)
    sma_days = int(cfg.get("sma_days") or SMA_DAYS)
    entry_drop = float(cfg.get("entry_drop_pct") or ENTRY_DROP)

    if cfg.get("paper_only") is not True:
        try:
            book = _load(state_path, starting)
        except DeskStateError:
            book = _fresh(starting)
        book["state_path"] = str(state_path)
        return public_view(book, status="blocked", reason="paper_only")

    try:
        book = _load(state_path, starting)
    except DeskStateError as exc:
        empty = _fresh(starting)
        empty["state_path"] = str(state_path)
        return public_view(empty, status="blocked", reason=str(exc))

    book["paper_only"] = True
    book["state_path"] = str(state_path)
    if cfg.get("enabled") is not True:
        return public_view(book, status="disabled")

    moment = _as_ct(now)
    session = moment.date()
    alerts: list[dict[str, Any]] = []
    route = resolve_route("kotegawa", host=host, live_trading=live_trading, environ=environ)
    book["route"] = route["mode"]

    for trade in _open_trades(book):
        symbol = str(trade.get("symbol") or "")
        series = None if bars is None else bars.get(symbol) or bars.get(normalize_symbol(symbol))
        high, low, last, bid = _prices(symbol, list(series) if series else None, quotes, session)
        reason = _exit_reason(trade, high=high, low=low, now=moment, hold_sessions=hold_sessions)
        if reason is None or bid is None:
            continue
        fill = sell_fill(bid=bid, last=last)
        if fill is None:
            continue
        shares = int(trade.get("shares") or 0)
        proceeds = fill * shares
        cost = float(trade.get("cost") or 0)
        pnl = proceeds - cost
        trade["status"] = "closed"
        trade["exit"] = fill
        trade["exit_reason"] = reason
        trade["exit_at"] = moment.isoformat()
        trade["exit_session"] = session.isoformat()
        trade["pnl"] = pnl
        book["cash"] = float(book["cash"]) + proceeds
        text = (
            f"[KOTEGAWA] SELL {symbol} {shares} sh · Filled @ ${fill:.2f} (paper ledger) · {reason} · "
            f"P&L {pnl:+.2f}"
        )
        alert = _alert("SELL", trade, text)
        alerts.append(alert)
        _remember(book, alert)

    realized = _realized_on(book, session)
    halted = realized <= -abs(daily_loss)
    book["entries_halted"] = halted
    book["halt_reason"] = f"daily realized {realized:.2f}" if halted else None

    if not halted and _in_entry_window(moment):
        held = {str(row.get("symbol")) for row in _open_trades(book)}
        marks = {
            str(row.get("symbol")): _mark_for(
                row,
                quotes,
                None if bars is None else list(bars.get(str(row.get("symbol"))) or []),
            )
            for row in _open_trades(book)
        }
        for idea in candidates(bars, sma_days=sma_days, entry_drop=entry_drop):
            if len(_open_trades(book)) >= max_positions:
                break
            symbol = idea["symbol"]
            if symbol in held:
                continue
            quote = _quote(quotes, symbol)
            bar = idea["bar"]
            ask = _f(quote.get("ask"))
            bid = _f(quote.get("bid"))
            last = _f(quote.get("last")) or _f(bar.get("close"))
            if ask is None and bid is None:
                ask = last
            fill = buy_fill(bid=bid, ask=ask, last=last)
            if fill is None or fill <= 0:
                continue
            marked = equity_of(book, marks)
            budget = min(marked * slot_pct, float(book["cash"]))
            shares = int(budget // fill)
            if shares < 1:
                continue
            cost = shares * fill
            if cost > float(book["cash"]):
                continue
            if route["mode"] == "sandbox":
                submit_sandbox_order(
                    desk="kotegawa",
                    account_id=route["account_id"],
                    host=route["host"],
                    order={"side": "BUY", "symbol": symbol, "shares": shares, "limit": fill},
                    environ=environ,
                    transport=transport,
                )
                book["sandbox_orders"] = list(book.get("sandbox_orders") or []) + [
                    {
                        "symbol": symbol,
                        "side": "BUY",
                        "account_id": route["account_id"],
                        "host": route["host"],
                    }
                ]
            trade = {
                "id": f"kot-{symbol}-{session.isoformat()}-{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "shares": shares,
                "entry": fill,
                "cost": cost,
                "entry_at": moment.isoformat(),
                "entry_session": session.isoformat(),
                "status": "open",
                "deviation": idea["deviation"],
                "route": route["mode"],
            }
            book["cash"] = float(book["cash"]) - cost
            book["trades"].append(trade)
            held.add(symbol)
            marks[symbol] = last or fill
            text = f"[KOTEGAWA] BUY {symbol} {shares} sh · Filled @ ${fill:.2f} (paper ledger)"
            alert = _alert("BUY", trade, text)
            alerts.append(alert)
            _remember(book, alert)

    for trade in _open_trades(book):
        symbol = str(trade.get("symbol") or "")
        series = None if bars is None else list(bars.get(symbol) or bars.get(normalize_symbol(symbol)) or [])
        trade["last_mark"] = _mark_for(trade, quotes, series or None)

    save_desk_state(state_path, book)
    view = public_view(book, as_of=session)
    view["cycle_alerts"] = alerts
    return view
