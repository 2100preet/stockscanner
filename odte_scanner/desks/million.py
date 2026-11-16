"""Million Challenge paper desk. Options, $1,000 → $1,000,000.

Separate from the existing Challenge sleeve: own ledger, own daily loss
counter, own position. One option position at a time, and one paper entry
per session. Each entry risks 10% of this desk's equity, with a minimum of
one contract, and skips the trade when one contract would be more than 25%
of equity.

``vwap_reclaim`` is the only signal that fills. It buys a same-day near-ATM
call (nearest expiry if that name has no 0DTE) and exits at +50%, −40%,
60 minutes after the fill, or 2:55 PM CT, whichever comes first. It is not
held overnight. The 1DTE twin (+50% / −30% / 60 minutes), ``oversold_bounce``,
and ``challenge_quality`` are log-only: the ticket and its would-be exit are
recorded, cash does not move, and nothing is routed.

Exits on a quality ticket stay +25% / −20%, plus the 15:45 ET flatten on a
contract that expires today. Those exits are recorded on the log ticket.

Every mark, take-profit, stop, and exit uses the option contract's own
quote. The underlying quote is only a sanity bound. A missing quote, a
price above 20× the entry premium, or a price above half the underlying is
not a fill. The 2:55 PM CT flatten then closes at the last valid option
mark, or at 0 when the contract expired worthless.

New entries stop for the day after two losing closes or after realized P&L
reaches −20% of the equity this desk started the day with. Log-only tickets
do not change cash, do not count as those losers, and are not routed.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from odte_scanner.desks.fills import buy_fill, sell_fill
from odte_scanner.desks.routing import resolve_route, submit_sandbox_order
from odte_scanner.desks.signals import build_signals
from odte_scanner.desks.store import DeskStateError, load_desk_state, save_desk_state
from odte_scanner.signals.hold_rules import expiry_is_today, past_odte_flatten

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
DESK = "million_challenge"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / "outputs" / "million_challenge_state.json"

STARTING_EQUITY = 1_000.0
SLOT_PCT = 0.10
MAX_CONTRACT_PCT = 0.25
QUALITY_TP = 0.25
QUALITY_STOP = 0.20
OVERSOLD_TARGET = 50.0
OVERSOLD_STOP = 40.0
OVERSOLD_FLATTEN_CT = "14:55"
VWAP_TARGET = 50.0
VWAP_STOP = 40.0
VWAP_TWIN_TARGET = 50.0
VWAP_TWIN_STOP = 30.0
VWAP_TIME_STOP_MINUTES = 60.0
VWAP_FLATTEN_CT = "14:55"
LOG_ONLY_SOURCES = {"oversold_bounce", "vwap_reclaim_1dte", "challenge_quality"}
DAILY_LOSERS = 2
DAILY_LOSS_PCT = 0.20
# A near-ATM premium cannot print at the stock price. These bounds refuse
# that quote instead of booking it. 20× entry is above every take-profit
# this desk uses. Half the underlying is above any near-ATM premium and is
# always below the stock price itself.
MARK_ENTRY_MULTIPLE = 20.0
MARK_UNDERLYING_FRACTION = 0.5
FLATTEN_REASONS = frozenset({"session_flat", "flatten_0dte"})
TARGET_DOUBLINGS = round(math.log(1_000_000 / STARTING_EQUITY, 2), 2)


def milestone_levels(start: float = STARTING_EQUITY) -> list[float]:
    """$2k, $4k, …, $1,024k."""
    level = float(start)
    out: list[float] = []
    for _ in range(10):
        level *= 2.0
        out.append(level)
    return out


def doublings_of(equity: float, start: float = STARTING_EQUITY) -> float:
    if equity <= 0 or start <= 0:
        return 0.0
    return math.log(equity / start, 2)


def _pct(value: Any, default: float) -> float:
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError):
        number = default
    return abs(number)


def _as_ct(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(CT)
    if now.tzinfo is None:
        return now.replace(tzinfo=CT)
    return now.astimezone(CT)


def _flatten_hm(value: Any) -> tuple[int, int]:
    try:
        hour, minute = str(value or OVERSOLD_FLATTEN_CT).split(":")
        return int(hour), int(minute)
    except (TypeError, ValueError):
        return (14, 55)


def _parse_instant(value: Any) -> datetime | None:
    """Parse a fill time and keep its instant.

    Aware values convert to Chicago. Naive values are Chicago, which is how
    this desk writes ``entry_at``. A UTC offset is not read as a CT clock.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CT)
    return parsed.astimezone(CT)


def _minutes_since(entry_at: Any, now: datetime) -> float | None:
    started = _parse_instant(entry_at)
    if started is None:
        return None
    return (_as_ct(now) - started).total_seconds() / 60.0


def _time_stop_due(trade: Mapping[str, Any], now: datetime, minutes: float) -> bool:
    elapsed = _minutes_since(trade.get("entry_at"), now)
    if elapsed is None:
        return False
    return elapsed + 1e-9 >= float(minutes)


def _minutes_setting(value: Any, default: float) -> float | None:
    try:
        number = float(default if value is None else value)
    except (TypeError, ValueError):
        number = float(default)
    if number <= 0:
        return None
    return number


def _session_flat_due(entry_session: str, now: datetime, flatten_ct: Any) -> bool:
    """Flatten at the entry session's close, by 2:55 PM CT.

    A position still open after that session is flattened on the next check.
    """
    try:
        started = date.fromisoformat(entry_session)
    except ValueError:
        return False
    ct = _as_ct(now)
    if ct.date() < started:
        return False
    if ct.date() > started:
        return True
    return (ct.hour, ct.minute) >= _flatten_hm(flatten_ct)


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _fresh(starting: float) -> dict[str, Any]:
    return {
        "desk": DESK,
        "paper_only": True,
        "starting_equity": starting,
        "cash": starting,
        "day": None,
        "day_start_equity": starting,
        "trades": [],
        "log_only": [],
        "recent_alerts": [],
        "sandbox_orders": [],
        "milestones": [{"level": level, "reached": False, "reached_at": None} for level in milestone_levels(starting)],
    }


def _file_epoch(saved: Mapping[str, Any] | None) -> int:
    """Epoch stored on a book. A missing or unreadable value is 0."""
    if not isinstance(saved, Mapping):
        return 0
    try:
        return int(saved.get("ledger_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def _configured_epoch(value: Any) -> int | None:
    """Positive config epoch, or None when this run must not reset."""
    if value is None or value is False:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _archive_state(path: Path) -> Path:
    """Rename the live book aside. The name keeps the stem and a timestamp."""
    stamp = datetime.now(CT).strftime("%Y%m%dT%H%M%S%f")
    archive = path.with_name(f"{path.stem}.{stamp}.bak.json")
    path.replace(archive)
    return archive


def _load(path: Path, starting: float, ledger_epoch: Any = None) -> dict[str, Any]:
    """Load this desk's book.

    When ``ledger_epoch`` is a positive integer and the file's epoch is
    older (a missing epoch is 0), the file is renamed to
    ``<stem>.<timestamp>.bak.json`` and the book starts over at ``starting``.
    A file tagged for another desk is not archived and still raises.
    """
    configured = _configured_epoch(ledger_epoch)
    saved = load_desk_state(path, DESK)
    if isinstance(saved, dict) and configured is not None and _file_epoch(saved) < configured:
        archive = _archive_state(path)
        book = _fresh(starting)
        book["ledger_epoch"] = configured
        save_desk_state(path, book)
        logger.warning(
            "million ledger epoch %s is older than %s; archived %s and reset to %.0f",
            _file_epoch(saved),
            configured,
            archive.name,
            starting,
        )
        return book
    if not isinstance(saved, dict):
        book = _fresh(starting)
        if configured is not None:
            book["ledger_epoch"] = configured
        return book
    book = _fresh(starting)
    book["starting_equity"] = float(saved.get("starting_equity") or starting)
    book["cash"] = float(saved.get("cash") if saved.get("cash") is not None else book["starting_equity"])
    book["day"] = saved.get("day")
    book["day_start_equity"] = float(saved.get("day_start_equity") or book["starting_equity"])
    book["trades"] = [dict(row) for row in saved.get("trades") or [] if isinstance(row, dict)]
    book["log_only"] = [dict(row) for row in saved.get("log_only") or [] if isinstance(row, dict)]
    book["recent_alerts"] = list(saved.get("recent_alerts") or [])[-30:]
    book["sandbox_orders"] = list(saved.get("sandbox_orders") or [])[-30:]
    known = {round(float(row["level"]), 2): row for row in book["milestones"]}
    for row in saved.get("milestones") or []:
        if not isinstance(row, dict):
            continue
        level = round(float(row.get("level") or 0), 2)
        if level in known and row.get("reached"):
            known[level]["reached"] = True
            known[level]["reached_at"] = row.get("reached_at")
    book["milestones"] = list(known.values())
    if configured is not None:
        book["ledger_epoch"] = max(configured, _file_epoch(saved))
    elif saved.get("ledger_epoch") is not None:
        book["ledger_epoch"] = _file_epoch(saved)
    return book


def _open_trades(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in book.get("trades") or [] if isinstance(row, dict) and row.get("status") == "open"]


def equity_of(book: Mapping[str, Any], marks: Mapping[str, float] | None = None) -> float:
    total = float(book.get("cash") or 0)
    for trade in _open_trades(book):
        key = str(trade.get("id") or "")
        mark = None if marks is None else marks.get(key)
        if mark is None:
            mark = float(trade.get("entry") or 0)
        total += int(trade.get("contracts") or 0) * 100.0 * float(mark)
    return total


def _positive(value: Any) -> float | None:
    number = _f(value)
    if number is None or number <= 0:
        return None
    return number


def _contract_quote(trade: Mapping[str, Any], quotes: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Quote stored for this OCC contract.

    The underlying symbol is never a key. Equity quotes live under that
    symbol in the same map, and using one of them prices the option at the
    stock.
    """
    if not isinstance(quotes, Mapping):
        return {}
    symbol = str(trade.get("symbol") or "")
    contract = str(trade.get("contract") or "").strip()
    keys: list[str] = []
    if contract and contract != symbol:
        keys.append(contract)
    trade_id = str(trade.get("id") or "").strip()
    if trade_id and trade_id not in {symbol, contract}:
        keys.append(trade_id)
    for key in keys:
        raw = quotes.get(key)
        if isinstance(raw, Mapping):
            return raw
    return {}


def _sell_basis(quote: Mapping[str, Any] | None) -> float | None:
    """Price a sell from the option quote: bid, else last, else ask, else mid."""
    if not isinstance(quote, Mapping):
        return None
    for key in ("bid", "last", "ask", "mid"):
        price = _positive(quote.get(key))
        if price is not None:
            return price
    return None


def _underlying_price(trade: Mapping[str, Any], quotes: Mapping[str, Any] | None) -> float | None:
    """Spot used only to reject an impossible option print.

    The live equity quote wins over the spot saved at entry. It is never
    returned as the option's own price.
    """
    symbol = str(trade.get("symbol") or "")
    contract = str(trade.get("contract") or "").strip()
    if isinstance(quotes, Mapping) and symbol and symbol != contract:
        raw = quotes.get(symbol)
        if isinstance(raw, Mapping):
            for key in ("last", "mid", "close", "bid", "ask"):
                price = _positive(raw.get(key))
                if price is not None:
                    return price
    for key in ("spot", "underlying", "underlying_price"):
        price = _positive(trade.get(key))
        if price is not None:
            return price
    return None


def _mark_rejection(price: float | None, entry: float, underlying: float | None) -> str | None:
    """Why this number must not be treated as an option premium."""
    if price is None or price <= 0:
        return "missing option quote"
    if entry > 0 and price > entry * MARK_ENTRY_MULTIPLE + 1e-9:
        return "option mark is more than 20x the entry premium"
    if (
        underlying is not None
        and underlying > 0
        and price > underlying * MARK_UNDERLYING_FRACTION + 1e-9
    ):
        return "option mark is more than half the underlying price"
    return None


def _option_mark(trade: Mapping[str, Any], quotes: Mapping[str, Any] | None) -> tuple[float | None, float | None]:
    """Return the option sell price, twice, when it passes the sanity guard.

    The first value marks the position. The second is the sell basis. Both
    are the contract bid (else last, else ask). A stock quote is not used.
    """
    basis = _sell_basis(_contract_quote(trade, quotes))
    if _mark_rejection(basis, float(trade.get("entry") or 0), _underlying_price(trade, quotes)):
        return None, None
    return basis, basis


def _expiry_instant(trade: Mapping[str, Any]) -> datetime | None:
    """4:00 PM ET on the expiry date. That instant is the contract's end."""
    text = str(trade.get("expiry") or "")[:10]
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)


def _contract_expired(trade: Mapping[str, Any], now: datetime) -> bool:
    """True once the contract is past its 4:00 PM ET expiry instant."""
    moment = _as_ct(now)
    expiry = _expiry_instant(trade)
    if expiry is not None:
        return moment >= expiry.astimezone(CT)
    if trade.get("dte") == 0:
        close = datetime(moment.year, moment.month, moment.day, 15, 0, tzinfo=CT)
        return moment >= close
    return False


def _expired_otm(trade: Mapping[str, Any], underlying: float | None) -> bool | None:
    """True when an expired contract finished out of the money.

    None means the strike or the spot is missing, so intrinsic is unknown.
    """
    if underlying is None:
        return None
    strike = _f(trade.get("strike"))
    if strike is None:
        return None
    right = str(trade.get("right") or "C").upper()[:1]
    if right == "P":
        return underlying + 1e-9 >= strike
    return underlying <= strike + 1e-9


def _flatten_fallback(trade: Mapping[str, Any], now: datetime, underlying: float | None) -> float:
    """Close price when the flatten has no usable option quote.

    An expired worthless contract closes at 0. Anything still alive, or an
    expired contract that finished in the money, closes at the last option
    mark that itself passed the guard. The underlying price is not a choice.
    """
    entry = float(trade.get("entry") or 0)
    if _contract_expired(trade, now):
        otm = _expired_otm(trade, underlying)
        if otm is None or otm:
            return 0.0
    for key in ("last_mark", "entry"):
        price = _positive(trade.get(key))
        if price is None:
            continue
        if _mark_rejection(price, entry, underlying):
            continue
        return price
    return 0.0


def contract_count(equity: float, fill: float, *, slot_pct: float, max_contract_pct: float) -> int | None:
    """10% of equity, at least one contract, unless one contract exceeds 25%."""
    if equity <= 0 or fill <= 0:
        return None
    one = fill * 100.0
    if one > equity * max_contract_pct:
        return None
    budget = equity * slot_pct
    count = int(budget // one)
    if count < 1:
        count = 1
    return count


def _losers_on(book: Mapping[str, Any], session: date) -> int:
    key = session.isoformat()
    count = 0
    for trade in book.get("trades") or []:
        if not isinstance(trade, dict) or trade.get("status") != "closed":
            continue
        if str(trade.get("exit_session") or "") != key:
            continue
        if float(trade.get("pnl") or 0) < 0:
            count += 1
    return count


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


def _stamp_milestones(book: dict[str, Any], equity: float, now: datetime) -> None:
    for row in book.get("milestones") or []:
        if row.get("reached"):
            continue
        if equity + 1e-6 >= float(row.get("level") or 0):
            row["reached"] = True
            row["reached_at"] = _as_ct(now).isoformat()


def _bracket_exit(
    trade: Mapping[str, Any],
    change: float | None,
    now: datetime,
    *,
    default_target: float,
    default_stop: float,
    default_time: float | None,
    flatten: bool,
) -> str | None:
    if change is not None:
        stop = _pct(trade.get("stop_pct"), default_stop) / 100.0
        target = _pct(trade.get("target_pct"), default_target) / 100.0
        if change <= -stop + 1e-12:
            return "hard_stop"
        if change + 1e-12 >= target:
            return "take_profit"
    if default_time is not None:
        span = _minutes_setting(trade.get("time_stop_minutes"), default_time)
        if span is not None and _time_stop_due(trade, now, span):
            return "time_stop"
    if flatten:
        clock = trade.get("flatten_ct") or VWAP_FLATTEN_CT
        if _session_flat_due(str(trade.get("entry_session") or ""), now, clock):
            return "session_flat"
        return None
    try:
        started = date.fromisoformat(str(trade.get("entry_session") or ""))
    except ValueError:
        return None
    if _as_ct(now).date() > started:
        return "session_flat"
    return None


def _exit_reason(trade: Mapping[str, Any], mark: float | None, *, now: datetime) -> str | None:
    """Why this ticket should close.

    A missing mark still allows the clock exits (time stop, session flatten,
    0DTE flatten). It cannot take a profit or stop, because those compare
    two option premiums.
    """
    entry = float(trade.get("entry") or 0)
    change: float | None = None
    if entry > 0 and mark is not None and mark > 0:
        change = (mark / entry) - 1.0
    source = str(trade.get("source") or "challenge_quality")
    if source == "vwap_reclaim":
        return _bracket_exit(
            trade,
            change,
            now,
            default_target=VWAP_TARGET,
            default_stop=VWAP_STOP,
            default_time=VWAP_TIME_STOP_MINUTES,
            flatten=True,
        )
    if source == "vwap_reclaim_1dte":
        return _bracket_exit(
            trade,
            change,
            now,
            default_target=VWAP_TWIN_TARGET,
            default_stop=VWAP_TWIN_STOP,
            default_time=VWAP_TIME_STOP_MINUTES,
            flatten=False,
        )
    if source == "oversold_bounce":
        return _bracket_exit(
            trade,
            change,
            now,
            default_target=OVERSOLD_TARGET,
            default_stop=OVERSOLD_STOP,
            default_time=None,
            flatten=True,
        )
    if change is not None and change + 1e-12 >= QUALITY_TP:
        return "take_profit"
    if change is not None and change <= -QUALITY_STOP + 1e-12:
        return "hard_stop"
    expiry = str(trade.get("expiry") or "")
    dte = trade.get("dte")
    if expiry and expiry_is_today(expiry, now) and past_odte_flatten(now):
        return "flatten_0dte"
    if not expiry and dte == 0 and past_odte_flatten(now):
        return "flatten_0dte"
    return None


def _is_log_only(idea: Mapping[str, Any]) -> bool:
    if idea.get("log_only") is True:
        return True
    return str(idea.get("source") or "") in LOG_ONLY_SOURCES


def _entered_today(book: Mapping[str, Any], session: date) -> bool:
    key = session.isoformat()
    for trade in book.get("trades") or []:
        if not isinstance(trade, dict) or trade.get("log_only") is True:
            continue
        if str(trade.get("entry_session") or "") == key:
            return True
    return False


def _logged_today(book: Mapping[str, Any], source: str, symbol: str, session: date) -> bool:
    key = session.isoformat()
    for row in book.get("log_only") or []:
        if not isinstance(row, dict):
            continue
        if row.get("source") == source and row.get("symbol") == symbol and row.get("entry_session") == key:
            return True
    return False


def vix_tag(vix: Mapping[str, Any] | None) -> dict[str, Any]:
    """Level and up/down versus the prior close. Never a filter."""
    if not isinstance(vix, Mapping):
        return {"vix": None, "vix_vs_prior": None}
    level = _f(vix.get("level"))
    if level is None:
        level = _f(vix.get("last"))
    if level is None:
        level = _f(vix.get("close"))
    prior = _f(vix.get("prior_close"))
    if prior is None:
        prior = _f(vix.get("prev_close"))
    if prior is None:
        prior = _f(vix.get("previous_close"))
    direction: str | None = None
    if level is not None and prior is not None:
        if level > prior + 1e-9:
            direction = "up"
        elif level < prior - 1e-9:
            direction = "down"
        else:
            direction = "unchanged"
    return {"vix": level, "vix_vs_prior": direction}


def _vix_text(tag: Mapping[str, Any]) -> str:
    level = tag.get("vix")
    if level is None:
        return ""
    direction = tag.get("vix_vs_prior") or "n/a"
    return f" · VIX {float(level):.2f} {direction}"


def _prefix(source: Any) -> str:
    if str(source or "").startswith("vwap_reclaim"):
        return "[MILLION][VWAP]"
    return "[MILLION]"


def _alert_session(side: str, trade: Mapping[str, Any]) -> str | None:
    raw = trade.get("exit_session") if side == "SELL" else trade.get("entry_session")
    text = str(raw or "")[:10]
    return text or None


def _alert(side: str, trade: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Fill notice for the portal and Telegram. Log-only tickets never call this."""
    return {
        "key": f"MILLION|{side}|{trade.get('id')}",
        "side": side,
        "desk": "Million",
        "symbol": trade.get("symbol"),
        "action": side,
        "source": trade.get("source"),
        "contract": trade.get("contract"),
        "expiry": trade.get("expiry"),
        "strike": trade.get("strike"),
        "right": trade.get("right"),
        "message": text,
        "session": _alert_session(side, trade),
        "paper": True,
        "log_only": False,
        "contracts": trade.get("contracts"),
        "entry": trade.get("entry"),
        "exit": trade.get("exit"),
        "ask": trade.get("entry") if side == "BUY" else None,
        "bid": trade.get("exit") if side == "SELL" else None,
        "row": {
            "symbol": trade.get("symbol"),
            "id": trade.get("id"),
            "contract": trade.get("contract"),
            "source": trade.get("source"),
            "right": trade.get("right"),
        },
    }


def _remember(book: dict[str, Any], alert: dict[str, Any]) -> None:
    book["recent_alerts"] = (list(book.get("recent_alerts") or []) + [alert])[-30:]


def _roll_day(book: dict[str, Any], session: date, quotes: Mapping[str, Any] | None) -> None:
    key = session.isoformat()
    if book.get("day") == key:
        return
    marks = {}
    for trade in _open_trades(book):
        mark, _bid = _option_mark(trade, quotes)
        if mark is not None:
            marks[str(trade.get("id"))] = mark
    book["day"] = key
    book["day_start_equity"] = equity_of(book, marks)


def _open_position(row: Mapping[str, Any]) -> dict[str, Any]:
    entry = _f(row.get("entry"))
    mark = _f(row.get("last_mark"))
    contracts = int(row.get("contracts") or 0)
    unrealized = None
    if entry is not None and mark is not None and contracts > 0:
        unrealized = round((mark - entry) * 100.0 * contracts, 2)
    return {
        "symbol": row.get("symbol"),
        "source": row.get("source"),
        "contracts": row.get("contracts"),
        "contract": row.get("contract"),
        "entry": row.get("entry"),
        "expiry": row.get("expiry"),
        "strike": row.get("strike"),
        "right": row.get("right"),
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
    equity = equity_of(book)
    win_rate = (wins / len(closed)) if closed else None
    reached = [row for row in book.get("milestones") or [] if row.get("reached")]
    day = as_of or _as_ct(None).date()
    return {
        "desk": DESK,
        "name": "Million Challenge",
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
        "doublings": round(doublings_of(equity, float(book.get("starting_equity") or STARTING_EQUITY)), 4),
        "target_doublings": TARGET_DOUBLINGS,
        "milestones": list(book.get("milestones") or []),
        "milestones_reached": len(reached),
        "signals": list(book.get("signals") or []),
        "log_only": [
            {
                "symbol": row.get("symbol"),
                "source": row.get("source"),
                "status": row.get("status"),
                "contract": row.get("contract"),
                "contracts": row.get("contracts"),
                "entry": row.get("entry"),
                "exit_reason": row.get("exit_reason"),
                "pnl": row.get("pnl"),
                "vix": row.get("vix"),
                "vix_vs_prior": row.get("vix_vs_prior"),
                "dte": row.get("dte"),
                "entry_session": row.get("entry_session"),
                "log_only": True,
            }
            for row in book.get("log_only") or []
            if isinstance(row, dict)
        ],
        "alerts": list(book.get("recent_alerts") or []),
        "state_path": book.get("state_path"),
        "ledger_epoch": book.get("ledger_epoch"),
    }


def scorecard_row(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "desk": "Million",
        "trades": int(view.get("trades") or 0),
        "win_rate": view.get("win_rate"),
        "realized_pnl": view.get("realized_pnl"),
        "open_positions": int(view.get("open_positions") or 0),
        "equity": view.get("equity"),
        "doublings": view.get("doublings"),
        "target_doublings": view.get("target_doublings"),
    }


def _record_log(
    book: dict[str, Any],
    idea: Mapping[str, Any],
    *,
    now: datetime,
    session: date,
    slot_pct: float,
    max_contract_pct: float,
    tag: Mapping[str, Any],
    equity: float,
    cash: float,
) -> dict[str, Any] | None:
    """Remember a ticket that is not filled and does not move cash."""
    source = str(idea.get("source") or "")
    symbol = str(idea.get("symbol") or "")
    if not symbol or _logged_today(book, source, symbol, session):
        return None
    ask = _f(idea.get("ask"))
    bid = _f(idea.get("bid"))
    fill = buy_fill(bid=bid, ask=ask, last=ask)
    if fill is None:
        return None
    count = contract_count(equity, fill, slot_pct=slot_pct, max_contract_pct=max_contract_pct)
    status = "open"
    skip_reason = None
    if count is None:
        status = "skipped"
        skip_reason = "oversized"
        count = 0
    else:
        one = fill * 100.0
        while count > 0 and count * one > cash + 1e-9:
            count -= 1
        if count < 1:
            status = "skipped"
            skip_reason = "cash"
            count = 0
    cost = count * fill * 100.0
    ticket = {
        "id": f"mil-log-{symbol}-{session.isoformat()}-{uuid.uuid4().hex[:8]}",
        "source": source,
        "log_only": True,
        "symbol": symbol,
        "right": idea.get("right") or "C",
        "contract": idea.get("contract"),
        "expiry": idea.get("expiry"),
        "strike": idea.get("strike"),
        "dte": idea.get("dte"),
        "contracts": count,
        "entry": fill if status == "open" else None,
        "last_mark": fill if status == "open" else None,
        "cost": cost if status == "open" else 0.0,
        "target_pct": idea.get("target_pct"),
        "stop_pct": idea.get("stop_pct"),
        "time_stop_minutes": idea.get("time_stop_minutes"),
        "flatten_ct": idea.get("flatten_ct"),
        "entry_at": _as_ct(now).isoformat(),
        "entry_session": session.isoformat(),
        "status": status,
        "reason": skip_reason or idea.get("reason"),
        "route": "log_only",
        "vwap": idea.get("vwap"),
        "spot": idea.get("spot"),
        "vix": tag.get("vix"),
        "vix_vs_prior": tag.get("vix_vs_prior"),
    }
    book["log_only"] = list(book.get("log_only") or []) + [ticket]
    return ticket


def _default_quote_fetcher(trade: Mapping[str, Any]) -> dict[str, Any] | None:
    """Live bid/ask for this OCC contract. Tests do not hit the network.

    The result is the contract quote from Tradier or Polygon. It is not the
    underlying. A failure leaves the mark missing so the cycle skips the exit.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    contract = str(trade.get("contract") or "").strip()
    symbol = str(trade.get("symbol") or "")
    if not contract or contract == symbol:
        return None
    try:
        from odte_scanner.options.contract_quote import fetch_contract_quote

        quote = fetch_contract_quote(
            {
                "symbol": symbol,
                "contract": contract,
                "expiry": trade.get("expiry"),
                "strike": trade.get("strike"),
                "right": trade.get("right"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("million option quote fetch failed %s %s: %s", symbol, contract, exc)
        return None
    return quote if isinstance(quote, dict) else None


def _open_logs(book: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in book.get("log_only") or []
        if isinstance(row, dict) and row.get("status") == "open"
    ]


def _with_contract_quotes(
    book: Mapping[str, Any],
    quotes: Mapping[str, Any] | None,
    fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None,
) -> dict[str, Any]:
    """Copy the quote map and fill any missing OCC keys from ``fetcher``.

    Equity quotes stay under the underlying symbol so the sanity bound can
    read the spot. They are not copied onto the contract.
    """
    out: dict[str, Any] = dict(quotes) if isinstance(quotes, Mapping) else {}
    if fetcher is None:
        return out
    for ticket in [*_open_trades(book), *_open_logs(book)]:
        contract = str(ticket.get("contract") or "").strip()
        symbol = str(ticket.get("symbol") or "")
        if not contract or contract == symbol:
            continue
        if isinstance(out.get(contract), Mapping):
            continue
        try:
            fetched = fetcher(ticket)
        except Exception as exc:  # noqa: BLE001
            logger.warning("million option quote fetch failed %s %s: %s", symbol, contract, exc)
            continue
        if isinstance(fetched, Mapping):
            out[contract] = dict(fetched)
    return out


def _fill_exit(
    book: dict[str, Any],
    ticket: dict[str, Any],
    basis: float,
    reason: str,
    now: datetime,
    session: date,
    alerts: list[dict[str, Any]],
    *,
    moves_cash: bool,
) -> None:
    """Close one ticket at an option price.

    ``basis`` is the option premium before slippage. Zero is a worthless
    flatten and is not run through the bid fill. Log-only tickets record
    the same exit and do not touch cash, alerts, or the daily loss count.
    """
    contracts = int(ticket.get("contracts") or 0)
    if basis <= 0:
        fill = 0.0
    else:
        filled = sell_fill(bid=basis)
        if filled is None:
            return
        fill = filled
    proceeds = fill * 100.0 * contracts
    pnl = proceeds - float(ticket.get("cost") or 0)
    moment = _as_ct(now)
    ticket["status"] = "closed"
    ticket["exit"] = fill
    ticket["exit_reason"] = reason
    ticket["exit_at"] = moment.isoformat()
    ticket["exit_session"] = session.isoformat()
    ticket["pnl"] = pnl
    if not moves_cash:
        return
    book["cash"] = float(book["cash"]) + proceeds
    text = (
        f"{_prefix(ticket.get('source'))} SELL {ticket.get('symbol')} {contracts}x @ {fill:.2f} · "
        f"{reason} · pnl {pnl:+.2f} · paper"
    )
    alert = _alert("SELL", ticket, text)
    alerts.append(alert)
    _remember(book, alert)


def _flatten_reason(trade: Mapping[str, Any], now: datetime) -> str | None:
    """Session flatten, independent of a time stop.

    A missing quote skips the time stop. The flatten still has to run, or a
    position opened more than an hour earlier would stay open at 2:55 PM CT
    because the time stop is checked first.
    """
    source = str(trade.get("source") or "challenge_quality")
    if source in {"vwap_reclaim", "oversold_bounce"}:
        clock = trade.get("flatten_ct") or VWAP_FLATTEN_CT
        if _session_flat_due(str(trade.get("entry_session") or ""), now, clock):
            return "session_flat"
        return None
    if source == "vwap_reclaim_1dte":
        try:
            started = date.fromisoformat(str(trade.get("entry_session") or ""))
        except ValueError:
            return None
        if _as_ct(now).date() > started:
            return "session_flat"
        return None
    expiry = str(trade.get("expiry") or "")
    dte = trade.get("dte")
    if expiry and expiry_is_today(expiry, now) and past_odte_flatten(now):
        return "flatten_0dte"
    if not expiry and dte == 0 and past_odte_flatten(now):
        return "flatten_0dte"
    return None


def _settle_open(
    book: dict[str, Any],
    ticket: dict[str, Any],
    *,
    quotes: Mapping[str, Any],
    now: datetime,
    session: date,
    alerts: list[dict[str, Any]],
    moves_cash: bool,
) -> None:
    """Mark one open option and exit it only on the contract's own quote."""
    if ticket.get("status") != "open":
        return
    entry = float(ticket.get("entry") or 0)
    if entry <= 0:
        return
    basis = _sell_basis(_contract_quote(ticket, quotes))
    underlying = _underlying_price(ticket, quotes)
    rejection = _mark_rejection(basis, entry, underlying)
    moment = _as_ct(now)
    if rejection is None and basis is not None:
        ticket["last_mark"] = basis
        if moves_cash:
            ticket["peak"] = max(float(ticket.get("peak") or entry), basis)
        reason = _exit_reason(ticket, basis, now=moment)
        if reason is None:
            return
        _fill_exit(book, ticket, basis, reason, moment, session, alerts, moves_cash=moves_cash)
        return
    reason = _flatten_reason(ticket, moment)
    label = f"{ticket.get('symbol')} {ticket.get('contract')}"
    if reason in FLATTEN_REASONS:
        fallback = _flatten_fallback(ticket, moment, underlying)
        logger.warning(
            "million flatten without a usable option quote %s: %s; closing at %.4f",
            label,
            rejection,
            fallback,
        )
        _fill_exit(book, ticket, fallback, reason, moment, session, alerts, moves_cash=moves_cash)
        return
    logger.warning("million option mark refused %s: %s", label, rejection)


def run_million(
    section: Mapping[str, Any] | None,
    *,
    challenge: Mapping[str, Any] | None = None,
    bars: Mapping[str, list[Mapping[str, Any]]] | None = None,
    chains: Mapping[str, list[Mapping[str, Any]]] | None = None,
    intraday: Mapping[str, Mapping[str, Any]] | None = None,
    quotes: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    path: str | Path | None = None,
    host: str | None = None,
    live_trading: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Any = None,
    signals: list[str] | None = None,
    vix: Mapping[str, Any] | None = None,
    quote_fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Advance the Million book. Paper only. Returns the public snapshot.

    ``quote_fetcher`` loads the OCC quote when the caller did not pass one.
    The default fetcher is skipped under pytest. It never substitutes the
    underlying price.
    """
    cfg = dict(section or {})
    starting = float(cfg.get("starting_equity") or STARTING_EQUITY)
    state_path = Path(path or cfg.get("state_path") or DEFAULT_STATE)
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    slot_pct = float(cfg.get("slot_pct") or SLOT_PCT)
    max_contract_pct = float(cfg.get("max_contract_pct") or MAX_CONTRACT_PCT)
    daily_losers = int(cfg.get("daily_losers") or DAILY_LOSERS)
    daily_loss_pct = float(cfg.get("daily_loss_pct") or DAILY_LOSS_PCT)
    oversold_cfg = dict(cfg.get("oversold_bounce") or {})
    oversold_target = _pct(oversold_cfg.get("target_pct"), OVERSOLD_TARGET)
    oversold_stop = _pct(oversold_cfg.get("stop_pct"), OVERSOLD_STOP)
    oversold_flat = str(oversold_cfg.get("flatten_ct") or OVERSOLD_FLATTEN_CT)
    vwap_cfg = dict(cfg.get("vwap_reclaim") or {})
    twin_cfg = dict(cfg.get("vwap_reclaim_1dte") or {})
    source_names = list(signals if signals is not None else (cfg.get("signals") or []))
    tag = vix_tag(vix)

    epoch = cfg.get("ledger_epoch")
    if cfg.get("paper_only") is not True:
        try:
            book = _load(state_path, starting, ledger_epoch=epoch)
        except DeskStateError:
            book = _fresh(starting)
        book["state_path"] = str(state_path)
        return public_view(book, status="blocked", reason="paper_only")

    try:
        book = _load(state_path, starting, ledger_epoch=epoch)
    except DeskStateError as exc:
        empty = _fresh(starting)
        empty["state_path"] = str(state_path)
        return public_view(empty, status="blocked", reason=str(exc))

    book["paper_only"] = True
    book["state_path"] = str(state_path)
    book["signals"] = source_names
    if cfg.get("enabled") is not True:
        return public_view(book, status="disabled")

    moment = _as_ct(now)
    session = moment.date()
    alerts: list[dict[str, Any]] = []
    route = resolve_route("million", host=host, live_trading=live_trading, environ=environ)
    book["route"] = route["mode"]
    fetcher = _default_quote_fetcher if quote_fetcher is None else quote_fetcher
    marked = _with_contract_quotes(book, quotes, fetcher)
    _roll_day(book, session, marked)

    for trade in _open_trades(book):
        _settle_open(
            book,
            trade,
            quotes=marked,
            now=moment,
            session=session,
            alerts=alerts,
            moves_cash=True,
        )
    for ticket in _open_logs(book):
        _settle_open(
            book,
            ticket,
            quotes=marked,
            now=moment,
            session=session,
            alerts=alerts,
            moves_cash=False,
        )

    realized = _realized_on(book, session)
    losers = _losers_on(book, session)
    day_start = float(book.get("day_start_equity") or starting)
    loss_hit = realized <= -(abs(daily_loss_pct) * day_start)
    loser_hit = losers >= daily_losers
    halted = loss_hit or loser_hit
    book["entries_halted"] = halted
    if halted:
        book["halt_reason"] = "two losers" if loser_hit else f"daily realized {realized:.2f}"
    else:
        book["halt_reason"] = None

    if not halted:
        ideas, _ignored = build_signals(
            source_names,
            challenge=challenge,
            bars=bars,
            chains=chains,
            intraday=intraday,
            now=moment,
            oversold_settings=oversold_cfg,
            vwap_settings=vwap_cfg,
            twin_settings=twin_cfg,
        )
        budget_equity = equity_of(book)
        budget_cash = float(book["cash"])
        for idea in ideas:
            if _is_log_only(idea):
                logged = dict(idea)
                if logged.get("source") == "oversold_bounce":
                    logged["target_pct"] = oversold_target
                    logged["stop_pct"] = oversold_stop
                    logged["flatten_ct"] = oversold_flat
                    logged["time_stop_minutes"] = None
                _record_log(
                    book,
                    logged,
                    now=moment,
                    session=session,
                    slot_pct=slot_pct,
                    max_contract_pct=max_contract_pct,
                    tag=tag,
                    equity=budget_equity,
                    cash=budget_cash,
                )
                continue
            if _open_trades(book) or _entered_today(book, session):
                continue
            ask = _f(idea.get("ask"))
            bid = _f(idea.get("bid"))
            fill = buy_fill(bid=bid, ask=ask, last=ask)
            if fill is None:
                continue
            marked = equity_of(book)
            count = contract_count(marked, fill, slot_pct=slot_pct, max_contract_pct=max_contract_pct)
            if count is None:
                continue
            one = fill * 100.0
            while count > 0 and count * one > float(book["cash"]) + 1e-9:
                count -= 1
            if count < 1:
                continue
            cost = count * one
            if route["mode"] == "sandbox":
                submit_sandbox_order(
                    desk="million",
                    account_id=route["account_id"],
                    host=route["host"],
                    order={
                        "side": "BUY",
                        "symbol": idea.get("symbol"),
                        "contract": idea.get("contract"),
                        "contracts": count,
                        "limit": fill,
                    },
                    environ=environ,
                    transport=transport,
                )
                book["sandbox_orders"] = list(book.get("sandbox_orders") or []) + [
                    {
                        "symbol": idea.get("symbol"),
                        "side": "BUY",
                        "account_id": route["account_id"],
                        "host": route["host"],
                    }
                ]
            trade = {
                "id": f"mil-{idea.get('symbol')}-{session.isoformat()}-{uuid.uuid4().hex[:8]}",
                "source": idea.get("source"),
                "log_only": False,
                "symbol": idea.get("symbol"),
                "right": idea.get("right") or "C",
                "contract": idea.get("contract"),
                "expiry": idea.get("expiry"),
                "strike": idea.get("strike"),
                "dte": idea.get("dte"),
                "contracts": count,
                "entry": fill,
                "last_mark": fill,
                "spot": idea.get("spot"),
                "cost": cost,
                "peak": fill,
                "target_pct": idea.get("target_pct"),
                "stop_pct": idea.get("stop_pct"),
                "time_stop_minutes": idea.get("time_stop_minutes"),
                "flatten_ct": idea.get("flatten_ct"),
                "vwap": idea.get("vwap"),
                "entry_at": moment.isoformat(),
                "entry_session": session.isoformat(),
                "status": "open",
                "reason": idea.get("reason"),
                "route": route["mode"],
                "vix": tag.get("vix"),
                "vix_vs_prior": tag.get("vix_vs_prior"),
            }
            book["cash"] = float(book["cash"]) - cost
            book["trades"].append(trade)
            text = (
                f"{_prefix(trade['source'])} BUY {trade['symbol']} {count}x {trade['right']} @ {fill:.2f} · "
                f"{trade['source']}{_vix_text(tag)} · paper"
            )
            alert = _alert("BUY", trade, text)
            alerts.append(alert)
            _remember(book, alert)
            twin = idea.get("twin")
            if isinstance(twin, Mapping):
                _record_log(
                    book,
                    twin,
                    now=moment,
                    session=session,
                    slot_pct=slot_pct,
                    max_contract_pct=max_contract_pct,
                    tag=tag,
                    equity=budget_equity,
                    cash=budget_cash,
                )

    _stamp_milestones(book, equity_of(book), moment)
    save_desk_state(state_path, book)
    view = public_view(book, as_of=session)
    view["cycle_alerts"] = alerts
    return view
