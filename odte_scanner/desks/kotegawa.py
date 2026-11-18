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

import logging
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.fills import buy_fill, sell_fill
from odte_scanner.desks.routing import resolve_route, submit_sandbox_order
from odte_scanner.desks.sp100 import in_sp100, normalize_symbol
from odte_scanner.desks.store import DeskStateError, load_desk_state, save_desk_state
from odte_scanner.desks import webull_desk

logger = logging.getLogger(__name__)

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


def _at_or_after_close(now: datetime | None, clock: tuple[int, int] = (15, 0)) -> bool:
    ct = _as_ct(now)
    return (ct.hour, ct.minute) >= clock


# Routed to Webull paper, the day-N time exit goes out at 2:50 PM CT so the
# sell can fill before the 3:00 PM CT close (the walk to the bid needs time).
WEBULL_TIME_EXIT = (14, 50)


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
    for key in ("era", "era_since", "simulated"):
        if saved.get(key) is not None:
            book[key] = saved.get(key)
    return book


def _open_trades(book: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in book["trades"] if row.get("status") == "open"]


def _active_trades(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Held stocks, a working Webull sell, or a working Webull buy."""
    return [
        row
        for row in book.get("trades") or []
        if isinstance(row, dict) and row.get("status") in {"open", "closing", "pending"}
    ]


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
    # No price this cycle: keep the last good mark rather than snapping the
    # position back to its entry price.
    kept = _f(trade.get("last_mark"))
    if kept is not None and kept > 0:
        return kept
    return float(trade.get("entry") or 0)


def _held_mark(trade: Mapping[str, Any]) -> float:
    """Saved mark for a position, or its entry when it was never marked."""
    mark = _f(trade.get("last_mark"))
    if mark is not None and mark > 0:
        return mark
    return float(trade.get("entry") or 0)


def equity_of(book: Mapping[str, Any], marks: Mapping[str, float] | None = None) -> float:
    total = float(book.get("cash") or 0)
    for trade in book.get("trades") or []:
        if not isinstance(trade, dict) or trade.get("status") not in {"open", "closing"}:
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
    close_clock: tuple[int, int] = (15, 0),
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
    if held == hold_sessions and _at_or_after_close(now, close_clock):
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


def _nth_session(start: date, n: int) -> date:
    """The n-th weekday counting ``start`` as session 1."""
    cursor = start
    count = 1 if start.weekday() < 5 else 0
    while count < n:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return cursor


def _open_position(row: Mapping[str, Any], *, hold_sessions: int = HOLD_SESSIONS) -> dict[str, Any]:
    entry = _f(row.get("entry"))
    mark = _f(row.get("last_mark"))
    shares = int(row.get("shares") or 0)
    unrealized = None
    if entry is not None and mark is not None and shares > 0:
        unrealized = round((mark - entry) * shares, 2)
    symbol = row.get("symbol")
    contract = f"{symbol} {shares} sh" if symbol and shares else symbol
    time_exit = None
    try:
        time_exit = _nth_session(date.fromisoformat(str(row.get("entry_session"))[:10]), hold_sessions).isoformat()
    except ValueError:
        pass
    return {
        "symbol": symbol,
        "shares": row.get("shares"),
        "contract": contract,
        "entry": row.get("entry"),
        "entry_at": row.get("entry_at"),
        "entry_session": row.get("entry_session"),
        "mark": mark,
        "marked_at": row.get("last_mark_at"),
        "unrealized_pnl": unrealized,
        "target": round(entry * (1.0 + TAKE_PROFIT), 2) if entry else None,
        "stop": round(entry * (1.0 - DISASTER_STOP), 2) if entry else None,
        "time_exit": time_exit,
        "status": row.get("status"),
        "fill_source": row.get("fill_source") or "simulated (internal ledger)",
        "order_label": webull_desk.working_label(row)
        or (f"Filled @ ${float(entry):.2f}" if entry is not None else None),
    }


def _working_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "shares": row.get("target_qty") or row.get("shares"),
        "status": row.get("status"),
        "order_label": webull_desk.working_label(row),
        "signal_ask": row.get("signal_ask"),
        "signal_at": row.get("signal_at"),
        "reason": row.get("not_filled_reason"),
        "fill_source": webull_desk.FILL_SOURCE,
    }


def _simulated_view(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    trades = [row for row in raw.get("trades") or [] if isinstance(row, Mapping)]
    return {
        "label": raw.get("label") or "Simulated (internal ledger, not Webull fills)",
        "archived_at": raw.get("archived_at"),
        "note": raw.get("note"),
        "realized_pnl": raw.get("realized_pnl"),
        "ending_equity": raw.get("ending_equity"),
        "trades": [
            {**_closed_trade(row), "fill_source": "simulated"} for row in trades if row.get("status") == "closed"
        ][:20],
    }


def _closed_trade(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = _f(row.get("entry"))
    exit_px = _f(row.get("exit"))
    shares = int(row.get("shares") or 0)
    pnl = _f(row.get("pnl"))
    cost = _f(row.get("cost")) or ((entry or 0) * shares)
    return {
        "symbol": row.get("symbol"),
        "shares": shares or None,
        "entry": entry,
        "entry_at": row.get("entry_at"),
        "exit": exit_px,
        "exit_at": row.get("exit_at"),
        "exit_reason": row.get("exit_reason"),
        "pnl": round(pnl, 2) if pnl is not None else None,
        "pnl_pct": round(pnl / cost * 100.0, 2) if pnl is not None and cost else None,
        "fill_source": row.get("exit_fill_source") or row.get("fill_source") or "simulated (internal ledger)",
        "order_label": f"Filled @ ${exit_px:.2f}" if exit_px is not None else None,
    }


CLOSED_SHOWN = 20


def strategy_summary(cfg: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Plain-words rules from the live config, for the portal tile."""
    c = dict(cfg or {})
    drop = float(c.get("entry_drop_pct") or ENTRY_DROP) * 100
    sma_days = int(c.get("sma_days") or SMA_DAYS)
    slots = int(c.get("max_positions") or MAX_POSITIONS)
    slot = float(c.get("slot_pct") or SLOT_PCT) * 100
    hold = int(c.get("hold_sessions") or HOLD_SESSIONS)
    loss = float(c.get("daily_loss_usd") or DAILY_LOSS_USD)
    start = float(c.get("starting_equity") or STARTING_EQUITY)
    return {
        "instrument": "Stocks (shares), long only",
        "universe": "Current S&P 100 names",
        "entry": (
            f"Buy a stock whose close is at least {drop:g}% below its {sma_days}-day average; "
            "deepest drop first. No SPY filter."
        ),
        "window": f"Entries 2:50–3:00 PM CT only. Holds overnight, up to {hold} trading days.",
        "exit": (
            f"Take profit +{TAKE_PROFIT * 100:g}% (intraday), disaster stop −{DISASTER_STOP * 100:g}%, "
            f"else sell at the close of trading day {hold} (entry day = day 1)."
        ),
        "sizing": f"Up to {slots} positions, {slot:g}% of desk equity each. Starts with ${start:,.0f}.",
        "risk": f"No new entries after −${loss:,.0f} realized in a day; exits still run.",
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
    opened = [row for row in trades if row.get("status") in {"open", "closing"}]
    working = [row for row in trades if row.get("status") == "pending"]
    not_filled = [row for row in trades if row.get("status") == "not_filled"]
    wins = sum(1 for row in closed if float(row.get("pnl") or 0) > 0)
    losses = sum(1 for row in closed if float(row.get("pnl") or 0) < 0)
    realized = sum(float(row.get("pnl") or 0) for row in closed)
    # Equity is cash plus each open position at its last saved mark. The cycle
    # refreshes that mark from the live quote; a missing quote keeps the
    # previous mark (never the entry price once the position has been marked).
    marks = {str(row.get("symbol")): _held_mark(row) for row in opened}
    equity = equity_of(book, marks)
    unrealized = sum(
        (_held_mark(row) - float(row.get("entry") or 0)) * float(row.get("shares") or 0) for row in opened
    )
    marked_at = max((str(row.get("last_mark_at") or "") for row in opened), default="") or None
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
        "fills": "webull" if book.get("era") == "webull" else ("switching" if book.get("route") == "sandbox" else "simulated"),
        "fill_source": webull_desk.FILL_SOURCE if book.get("era") == "webull" else "simulated (internal ledger)",
        "era_since": book.get("era_since"),
        "working": [_working_row(row) for row in working],
        "not_filled": [_working_row(row) for row in not_filled[-5:]],
        "simulated": _simulated_view(book.get("simulated")),
        "equity": round(equity, 2),
        "cash": round(float(book.get("cash") or 0), 2),
        "starting_equity": float(book.get("starting_equity") or STARTING_EQUITY),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "marked_at": marked_at,
        "realized_today": round(_realized_on(book, day), 2),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "trades": len(closed),
        "open_positions": len(opened),
        "open": [_open_position(row) for row in opened],
        "closed": [
            _closed_trade(row)
            for row in sorted(closed, key=lambda r: str(r.get("exit_at") or ""), reverse=True)[:CLOSED_SHOWN]
        ],
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


def _webull_alert(side: str, trade: Mapping[str, Any], text: str, event: str) -> dict[str, Any]:
    alert = _alert(side, trade, text)
    alert["key"] = f"KOTEGAWA|{side}|{trade.get('id')}|{event}"
    alert["fill_source"] = webull_desk.FILL_SOURCE
    alert["event"] = event
    return alert


def _spec(cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    orders = dict((cfg or {}).get("webull_orders") or {})
    return {
        "desk": "kotegawa",
        "instrument": "EQUITY",
        "mult": 1.0,
        "qty_key": "shares",
        "step_sec": float(orders.get("step_sec") or 15),
        "entry_chase_sec": float(orders.get("entry_chase_sec") or 240),
        "slippage": float(orders.get("slippage") or 0.003),
        "head": lambda trade: "[KOTEGAWA]",
        "text": lambda trade: str(trade.get("symbol") or ""),
        "alert": _webull_alert,
    }


def _entry_deadline(trade: Mapping[str, Any], now: datetime) -> bool:
    """A Kotegawa buy still working at 2:59 PM CT is cancelled before the close."""
    ct = _as_ct(now)
    return (ct.hour, ct.minute) >= (14, 59)


def _default_stock_quotes(symbols: list[str]) -> dict[str, Any]:
    """Live Tradier bid/ask for these stocks. Tests do not hit the network."""
    if os.environ.get("PYTEST_CURRENT_TEST") or not symbols:
        return {}
    try:
        from odte_scanner.data.tradier import fetch_quotes

        return dict(fetch_quotes(list(symbols), timeout=6.0).get("quotes") or {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("kotegawa live quotes failed: %s", exc)
        return {}


def _live_quote(symbol: str, quotes: Mapping[str, Any] | None, fetch: Any) -> dict[str, Any]:
    """Bid/ask from the cycle's quote map, else one live fetch."""
    got = dict(_quote(quotes, symbol))
    if _f(got.get("ask")) and _f(got.get("bid")):
        return got
    fresh = fetch([symbol]) if fetch else {}
    row = fresh.get(symbol) if isinstance(fresh, Mapping) else None
    return dict(row) if isinstance(row, Mapping) else got


SIMULATED_NOTE = (
    "Internal-ledger fills priced by the app (buy at the quote mid, sell at the bid), not by a broker. "
    "Checked against Tradier 1-minute bars, the Nov 16 2:51 PM CT entries sat inside or within ~0.3% of "
    "that minute's trades, so these stock fills were close to achievable."
)


def _switch_era(book: dict[str, Any], starting: float, now: datetime) -> bool:
    """Start the Webull-paper book once the last simulated position has closed."""
    if book.get("era") == "webull":
        return False
    if any(row.get("route") != "webull" for row in _active_trades(book)):
        return False
    old = [dict(row) for row in book.get("trades") or [] if isinstance(row, dict)]
    if old:
        book["simulated"] = {
            "label": "Simulated (internal ledger, not Webull fills)",
            "archived_at": _as_ct(now).isoformat(),
            "note": SIMULATED_NOTE,
            "realized_pnl": round(sum(float(r.get("pnl") or 0) for r in old if r.get("status") == "closed"), 2),
            "ending_equity": round(float(book.get("cash") or 0), 2),
            "trades": [{**row, "fill_source": "simulated"} for row in old],
        }
    book["trades"] = []
    book["cash"] = float(starting)
    book["recent_alerts"] = [a for a in book.get("recent_alerts") or [] if str(a.get("session") or "") == _as_ct(now).date().isoformat()]
    book["era"] = "webull"
    book["era_since"] = _as_ct(now).isoformat()
    logger.warning("kotegawa: switched to Webull paper fills; simulated history archived in the book")
    return True


def advance_webull(
    section: Mapping[str, Any] | None,
    *,
    live_trading: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    broker: Any = None,
    quote_fetcher: Any = None,
) -> dict[str, Any]:
    """Fast tick: refresh, walk and settle working Webull orders."""
    cfg = dict(section or {})
    if cfg.get("enabled") is not True or cfg.get("paper_only") is not True:
        return {"skipped": "disabled"}
    starting = float(cfg.get("starting_equity") or STARTING_EQUITY)
    state_path = Path(cfg.get("state_path") or DEFAULT_STATE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not state_path.exists():
        return {"skipped": "no book"}
    book = _load(state_path, starting)
    if not any(row.get("status") in webull_desk.WORKING for row in book.get("trades") or []):
        return {"working": 0}
    route = resolve_route("kotegawa", live_trading=live_trading, environ=environ)
    use = broker or webull_desk.desk_broker("kotegawa", route, live_trading=live_trading)
    if use is None:
        return {"skipped": "not routed"}
    fetch = quote_fetcher or _default_stock_quotes
    alerts = webull_desk.advance(
        book,
        use,
        _spec(cfg),
        quote_fn=lambda t: _live_quote(str(t.get("symbol") or ""), None, fetch),
        now=now,
        entry_deadline=_entry_deadline,
    )
    for alert in alerts:
        _remember(book, alert)
    book["state_path"] = str(state_path)
    save_desk_state(state_path, book)
    return {"alerts": len(alerts)}


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
    broker: Any = None,
    quote_fetcher: Any = None,
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
    wb = None
    if route["mode"] == "sandbox":
        wb = broker or webull_desk.desk_broker("kotegawa", route, live_trading=live_trading)
        if wb is None:
            route = {**route, "mode": "internal", "reason": "Webull paper submit is off"}
    book["route"] = route["mode"]
    book["route_reason"] = route.get("reason")
    fetch = quote_fetcher or _default_stock_quotes
    spec = _spec(cfg)
    close_clock = WEBULL_TIME_EXIT if wb is not None else (15, 0)
    if wb is not None:
        for alert in webull_desk.advance(
            book, wb, spec, quote_fn=lambda t: _live_quote(str(t.get("symbol") or ""), quotes, fetch),
            now=moment, entry_deadline=_entry_deadline,
        ):
            alerts.append(alert)
            _remember(book, alert)

    for trade in _open_trades(book):
        symbol = str(trade.get("symbol") or "")
        series = None if bars is None else bars.get(symbol) or bars.get(normalize_symbol(symbol))
        if wb is not None and trade.get("route") == "webull":
            # A Webull position exits on what it can actually sell for now:
            # the live bid, not the day's bar high/low.
            live = _live_quote(symbol, quotes, fetch)
            live_bid = _f(live.get("bid"))
            reason = _exit_reason(
                trade, high=live_bid, low=live_bid, now=moment, hold_sessions=hold_sessions, close_clock=close_clock
            )
            if reason is None:
                continue
            alert = webull_desk.open_exit(
                book, wb, spec, trade, reason, bid=live_bid, ask=_f(live.get("ask")),
                mark=_f(live.get("last")) or trade.get("last_mark"), now=moment,
            )
            if alert:
                alerts.append(alert)
                _remember(book, alert)
            continue
        high, low, last, bid = _prices(symbol, list(series) if series else None, quotes, session)
        reason = _exit_reason(
            trade, high=high, low=low, now=moment, hold_sessions=hold_sessions, close_clock=close_clock
        )
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

    if wb is not None:
        _switch_era(book, starting, moment)
    realized = _realized_on(book, session)
    halted = realized <= -abs(daily_loss)
    book["entries_halted"] = halted
    book["halt_reason"] = f"daily realized {realized:.2f}" if halted else None

    routed_entries = wb is not None and book.get("era") == "webull"
    if wb is not None and not routed_entries:
        logger.info("kotegawa: simulated positions still open; Webull entries start after they close")
    if routed_entries and _in_entry_window(moment):
        from odte_scanner.trading.risk_controls import kill_switch_state

        if kill_switch_state().get("on"):
            book["halt_reason"] = "paper kill switch"
            halted = True
    if routed_entries and not halted and _in_entry_window(moment):
        held = {str(row.get("symbol")) for row in _active_trades(book)}
        marks = {
            str(row.get("symbol")): _mark_for(
                row,
                quotes,
                None if bars is None else list(bars.get(str(row.get("symbol"))) or []),
            )
            for row in _active_trades(book)
        }
        for idea in candidates(bars, sma_days=sma_days, entry_drop=entry_drop):
            if len(_active_trades(book)) >= max_positions:
                break
            symbol = idea["symbol"]
            if symbol in held:
                continue
            live = _live_quote(symbol, quotes, fetch)
            ask = _f(live.get("ask"))
            bid = _f(live.get("bid"))
            if ask is None or ask <= 0:
                logger.info("kotegawa entry skipped %s: no live ask", symbol)
                continue
            budget = min(
                equity_of(book, marks) * slot_pct,
                float(book["cash"]) - webull_desk.pending_cost(book, spec),
            )
            shares = int(budget // ask)
            if shares < 1:
                continue
            trade = {
                "id": f"kot-{symbol}-{session.isoformat()}-{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "shares": shares,
                "entry_session": session.isoformat(),
                "deviation": idea["deviation"],
            }
            alert = webull_desk.open_entry(book, wb, spec, trade, bid=bid, ask=ask, now=moment)
            if alert:
                alerts.append(alert)
                _remember(book, alert)
                held.add(symbol)
                marks[symbol] = ask
    if wb is None and not halted and _in_entry_window(moment):
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

    for trade in [row for row in book["trades"] if row.get("status") in {"open", "closing"}]:
        symbol = str(trade.get("symbol") or "")
        series = None if bars is None else list(bars.get(symbol) or bars.get(normalize_symbol(symbol)) or [])
        mark = _mark_for(trade, quotes, series or None)
        if mark != _f(trade.get("last_mark")):
            trade["last_mark_at"] = moment.isoformat()
        trade["last_mark"] = mark

    save_desk_state(state_path, book)
    view = public_view(book, as_of=session)
    view["cycle_alerts"] = alerts
    return view
