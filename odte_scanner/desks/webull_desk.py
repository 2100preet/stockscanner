"""Webull PAPER order path for the isolated desks (Million, Kotegawa).

Webull paper fills are the source of truth for these books. A desk entry is
an order on the Webull sandbox; the book records a position only after
Webull reports a fill, at Webull's fill price. Exits are the same: the book
closes a trade at the Webull sell fill.

Isolation from the main desks (Gold / Sniper / Diamond / Afternoon / Lunch):

* Each desk has its own order ledger (``outputs/<desk>_webull_orders.json``),
  never ``webull_orders.json``. The main daily tally, the −$800 account loss
  limit, the portal round trips and the Edge samples read that main ledger
  only, so these desks cannot trip, or be tripped by, the main account limit.
  Each desk keeps its own loss limits inside its own book.
* Client order ids start with ``mil-`` or ``kot-``. The main ledger's
  history import only adopts ``wb-`` ids, so these orders are never pulled in.
* ``isolated_holdings()`` lists the option lots these desks hold so the main
  AutoTrader subtracts them from the shared account's positions and never
  manages or sells them.

Order mechanics match the other desks: a buy starts near the ask (mid
rounded toward the ask), walks up to the live ask every ``step_sec`` within a
slippage cap, and is cancelled with a "not filled" alert when it is still
working after ``entry_chase_sec``. A sell starts at the mid for a take-profit
(at the bid for a stop or time exit) and walks down to the live bid until it
fills. It is never abandoned.

Sandbox host only. Nothing here can reach ``api.webull.com``; WebullBroker
refuses it, and the routing guard only hands out the desk's own account id.
SPX is never routed.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")
ROOT = Path(__file__).resolve().parents[2]

LEDGERS = {
    "million": ROOT / "outputs" / "million_webull_orders.json",
    "kotegawa": ROOT / "outputs" / "kotegawa_webull_orders.json",
}
PREFIX = {"million": "mil", "kotegawa": "kot"}
WORKING = {"pending", "closing"}
# How the portal and Telegram name a fill that came from Webull.
FILL_SOURCE = "Webull paper"

_BROKERS: dict[str, Any] = {}
_BROKER_LOCK = threading.Lock()


def _now_ct(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(CT)
    if now.tzinfo is None:
        return now.replace(tzinfo=CT)
    return now.astimezone(CT)


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number


def _age_sec(stamp: Any, now: datetime) -> float:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 1e9
    if when.tzinfo is None:
        when = when.replace(tzinfo=CT)
    return (now - when).total_seconds()


# ---- broker -----------------------------------------------------------------


def desk_broker(
    desk: str,
    route: Mapping[str, Any],
    *,
    live_trading: Mapping[str, Any] | None = None,
    ledger_path: str | Path | None = None,
    factory: Callable[..., Any] | None = None,
) -> Any | None:
    """WebullBroker on this desk's own ledger and account, or None.

    None when the route is not the sandbox, or when Webull paper submits are
    off (no keys / no WEBULL_PAPER_SUBMIT). The desk then stays on its
    internal ledger instead of pretending to be routed.
    """
    if route.get("mode") != "sandbox" or not route.get("account_id"):
        return None
    if str(route.get("host") or "") != "api.sandbox.webull.com":
        return None
    path = Path(ledger_path) if ledger_path else LEDGERS[desk]
    if factory is not None:
        return factory(account_id=route["account_id"], ledger_path=path)
    from odte_scanner.live_desk import resolve_webull_live_flags
    from odte_scanner.trading.webull import WebullBroker

    flags = resolve_webull_live_flags(dict(live_trading or {}))
    if not (flags.get("enabled") and not flags.get("dry_run")):
        return None
    key = f"{desk}|{route['account_id']}|{path}"
    with _BROKER_LOCK:
        broker = _BROKERS.get(key)
        if broker is None:
            broker = WebullBroker(
                enabled=True,
                dry_run=False,
                sandbox=True,
                paper=True,
                account_id=str(route["account_id"]),
                ledger_path=path,
                submit_interval_sec=0.5,
            )
            _BROKERS[key] = broker
        else:
            broker.reload()
    return broker


def _intent(broker: Any, order_id: str) -> Any | None:
    for order in getattr(broker, "orders", []) or []:
        if order.id == order_id:
            return order
    return None


def _confirm(broker: Any, intent: Any) -> None:
    """Read the order once more so a fill that raced a cancel is counted."""
    if getattr(broker, "dry_run", False) or not getattr(broker, "enabled", False):
        return
    if not intent.broker_order_id:
        return
    try:
        from odte_scanner.trading.webull import classify_broker_order

        payload = broker._fetch_order_detail(intent)
        classified = classify_broker_order(payload)
        state = classified.get("fill_state")
        if state in {"filled", "partial", "cancelled"}:
            broker._apply_fill(intent, classified)
            if state == "partial" and intent.status == "partial":
                # Cancelled with a partial fill: the rest is no longer working.
                intent.meta["fill_state"] = "partial_cancelled"
            broker.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("isolated desk cancel check failed %s: %s", intent.id, exc)


# ---- prices -----------------------------------------------------------------


def _option_tick_buy(bid: float | None, ask: float) -> float:
    from odte_scanner.trading.ticks import initial_buy_limit

    return initial_buy_limit(bid, ask)


def initial_limit(spec: Mapping[str, Any], side: str, *, bid: float | None, ask: float | None, urgent: bool) -> float | None:
    """First limit. Buy: mid rounded toward the ask. Sell: mid (take-profit) or bid."""
    equity = spec.get("instrument") == "EQUITY"
    bid_f, ask_f = _f(bid), _f(ask)
    if side == "BUY":
        if ask_f is None:
            return None
        if equity:
            if bid_f is None or bid_f > ask_f:
                return round(ask_f, 2)
            mid = (bid_f + ask_f) / 2.0
            return round(min(ask_f, int(mid * 100 + 0.999999) / 100.0), 2)
        return _option_tick_buy(bid_f, ask_f)
    if bid_f is None:
        return None
    if urgent or ask_f is None or ask_f < bid_f:
        target = bid_f
    else:
        target = (bid_f + ask_f) / 2.0
    if equity:
        return round(max(bid_f, int(target * 100) / 100.0), 2)
    from odte_scanner.trading.ticks import normalize_option_limit

    snapped = normalize_option_limit(target, side="SELL")
    return snapped if snapped >= bid_f - 1e-9 else normalize_option_limit(bid_f, side="SELL")


def next_buy_limit(spec: Mapping[str, Any], *, ask: float | None, signal_ask: float | None, limit: float) -> float | None:
    """Walk a working buy up to the live ask, capped at slippage over the signal ask."""
    ask_f = _f(ask)
    if ask_f is None:
        return None
    if spec.get("instrument") == "EQUITY":
        base = _f(signal_ask) or ask_f
        cap = base * (1.0 + float(spec.get("slippage") or 0.003))
        target = round(min(ask_f, cap), 2)
        return target if target > limit + 0.001 else None
    from odte_scanner.trading.chase import reprice_limit

    return reprice_limit(ask_f, signal_ask, limit)


def next_sell_limit(spec: Mapping[str, Any], *, bid: float | None, limit: float) -> float | None:
    """A working sell moves to the live bid (down or up) once it has rested a step."""
    bid_f = _f(bid)
    if bid_f is None:
        return None
    if spec.get("instrument") == "EQUITY":
        target = round(bid_f, 2)
    else:
        from odte_scanner.trading.ticks import normalize_option_limit

        target = normalize_option_limit(bid_f, side="SELL")
    if target <= 0 or abs(target - limit) < 0.001:
        return None
    return target


# ---- orders on a trade --------------------------------------------------------


def _qty(trade: Mapping[str, Any], spec: Mapping[str, Any]) -> int:
    try:
        return int(trade.get(spec["qty_key"]) or 0)
    except (TypeError, ValueError):
        return 0


def _filled(trade: Mapping[str, Any], side: str) -> tuple[int, float]:
    """Filled quantity and quantity-weighted price across this side's orders."""
    qty = 0
    notional = 0.0
    for row in trade.get("orders") or []:
        if not isinstance(row, Mapping) or row.get("side") != side:
            continue
        got = int(float(row.get("filled_qty") or 0))
        px = _f(row.get("fill_price"))
        if got > 0 and px is not None:
            qty += got
            notional += got * px
    return qty, (notional / qty if qty else 0.0)


def working_label(trade: Mapping[str, Any]) -> str | None:
    """``Limit $x (working)`` while an order rests, ``Filled @ $x`` once filled."""
    work = trade.get("working")
    if isinstance(work, Mapping) and work.get("limit") is not None:
        return f"Limit ${float(work['limit']):.2f} (working)"
    return None


def _place(
    broker: Any,
    spec: Mapping[str, Any],
    trade: dict[str, Any],
    side: str,
    qty: int,
    limit: float,
    *,
    now: datetime,
    signal_px: float | None,
    reason: str,
) -> Any:
    desk = spec["desk"]
    meta: dict[str, Any] = {
        "isolated_desk": desk,
        "desk_trade_id": trade.get("id"),
        "account_id": getattr(broker, "account_id", None),
        "instrument": spec.get("instrument") or "OPTION",
    }
    if side == "BUY" and spec.get("instrument") != "EQUITY" and signal_px:
        meta["ask_cap"] = round(float(signal_px) * (1.0 + float(spec.get("slippage") or 0.10)), 2)
    intent = broker.stage(
        desk=desk,
        action=side,
        symbol=str(trade.get("symbol") or ""),
        right=str(trade.get("right") or "C"),
        contract=trade.get("contract") if spec.get("instrument") != "EQUITY" else None,
        expiry=trade.get("expiry") if spec.get("instrument") != "EQUITY" else None,
        strike=trade.get("strike") if spec.get("instrument") != "EQUITY" else None,
        quantity=qty,
        limit_price=limit,
        reason=reason,
        meta=meta,
    )
    # Desk-tagged client order id: never ``wb-``, so the main ledger's history
    # import cannot adopt it.
    intent.id = f"{PREFIX[desk]}-{uuid.uuid4().hex[:12]}"
    broker.save()
    broker.submit(intent)
    row = {
        "id": intent.id,
        "side": side,
        "qty": qty,
        "limit": intent.limit_price,
        "status": intent.status,
        "placed_at": now.isoformat(),
        "filled_qty": 0,
        "fill_price": None,
        "error": intent.error,
    }
    trade.setdefault("orders", []).append(row)
    trade["working"] = {
        "id": intent.id,
        "side": side,
        "limit": intent.limit_price,
        "placed_at": now.isoformat(),
        "first_at": (trade.get("working") or {}).get("first_at") if side == (trade.get("working") or {}).get("side") else now.isoformat(),
        "signal_px": signal_px,
    }
    if not trade["working"].get("first_at"):
        trade["working"]["first_at"] = now.isoformat()
    return intent


def _sync_row(trade: dict[str, Any], intent: Any) -> dict[str, Any] | None:
    for row in trade.get("orders") or []:
        if row.get("id") == intent.id:
            row["status"] = intent.status
            row["limit"] = intent.limit_price
            got = (intent.meta or {}).get("filled_qty")
            try:
                row["filled_qty"] = int(float(got or 0))
            except (TypeError, ValueError):
                row["filled_qty"] = 0
            if intent.status == "filled" and not row["filled_qty"]:
                row["filled_qty"] = int(intent.quantity or 0)
            row["fill_price"] = intent.fill_price
            row["error"] = intent.error
            return row
    return None


def open_entry(
    book: dict[str, Any],
    broker: Any,
    spec: Mapping[str, Any],
    trade: dict[str, Any],
    *,
    bid: float | None,
    ask: float | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Send the entry buy and park the trade as ``pending``. Returns an alert."""
    moment = _now_ct(now)
    qty = _qty(trade, spec)
    limit = initial_limit(spec, "BUY", bid=bid, ask=ask, urgent=False)
    if qty < 1 or limit is None or limit <= 0:
        return None
    trade["status"] = "pending"
    trade["route"] = "webull"
    trade["fill_source"] = FILL_SOURCE
    trade["target_qty"] = qty
    trade["signal_bid"] = _f(bid)
    trade["signal_ask"] = _f(ask)
    trade["signal_at"] = moment.isoformat()
    intent = _place(broker, spec, trade, "BUY", qty, limit, now=moment, signal_px=_f(ask), reason=str(trade.get("source") or spec["desk"]))
    book.setdefault("trades", []).append(trade)
    if intent.status in {"rejected", "cancelled", "skipped"}:
        trade["status"] = "not_filled"
        trade["working"] = None
        trade["not_filled_reason"] = intent.error or intent.status
        return spec["alert"](
            "BUY",
            trade,
            f"{spec['head'](trade)} BUY {spec['text'](trade)} ×{qty} · NOT FILLED · Webull paper {intent.status}: "
            f"{(intent.error or '')[:120]}",
            "notfilled",
        )
    return spec["alert"](
        "BUY",
        trade,
        f"{spec['head'](trade)} BUY {spec['text'](trade)} ×{qty} · Limit ${float(intent.limit_price):.2f} (working) · Webull paper",
        "working",
    )


def open_exit(
    book: dict[str, Any],
    broker: Any,
    spec: Mapping[str, Any],
    trade: dict[str, Any],
    reason: str,
    *,
    bid: float | None,
    ask: float | None,
    mark: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Send the exit sell. The trade stays marked until Webull fills it."""
    moment = _now_ct(now)
    qty = _qty(trade, spec)
    sold, _px = _filled(trade, "SELL")
    left = qty - sold
    if left < 1:
        return None
    urgent = reason != "take_profit"
    limit = initial_limit(spec, "SELL", bid=bid, ask=ask, urgent=urgent)
    if limit is None:
        fallback = _f(mark) or _f(trade.get("last_mark"))
        if fallback is None:
            return None
        limit = round(max(0.01, fallback * 0.9), 2)
    trade["status"] = "closing"
    trade["exit_reason"] = reason
    trade["exit_signal_at"] = moment.isoformat()
    trade["exit_signal_bid"] = _f(bid)
    trade["working"] = None
    intent = _place(broker, spec, trade, "SELL", left, limit, now=moment, signal_px=_f(bid), reason=reason)
    return spec["alert"](
        "SELL",
        trade,
        f"{spec['head'](trade)} SELL {spec['text'](trade)} ×{left} · Limit ${float(intent.limit_price):.2f} (working) · "
        f"{reason} · Webull paper",
        f"working{len(trade.get('orders') or [])}",
    )


def _finish_buy(book: dict[str, Any], spec: Mapping[str, Any], trade: dict[str, Any], moment: datetime) -> dict[str, Any]:
    got, avg = _filled(trade, "BUY")
    trade["working"] = None
    if got < 1:
        trade["status"] = "not_filled"
        trade["not_filled_reason"] = trade.get("not_filled_reason") or "entry chase ended without a Webull fill"
        last = (trade.get("orders") or [{}])[-1].get("limit")
        return spec["alert"](
            "BUY",
            trade,
            f"{spec['head'](trade)} BUY {spec['text'](trade)} · NOT FILLED · cancelled after the chase "
            f"(last limit ${float(last or 0):.2f}) · Webull paper",
            "notfilled",
        )
    mult = float(spec["mult"])
    trade[spec["qty_key"]] = got
    trade["entry"] = avg
    trade["cost"] = got * avg * mult
    trade["last_mark"] = avg
    trade["peak"] = avg
    trade["entry_at"] = moment.isoformat()
    trade["entry_session"] = moment.date().isoformat()
    trade["status"] = "open"
    book["cash"] = float(book.get("cash") or 0) - trade["cost"]
    part = "" if got >= int(trade.get("target_qty") or got) else f" (partial {got}/{trade.get('target_qty')})"
    return spec["alert"](
        "BUY",
        trade,
        f"{spec['head'](trade)} BUY {spec['text'](trade)} ×{got}{part} · Filled @ ${avg:.2f} (Webull paper)",
        "filled",
    )


def _finish_sell(book: dict[str, Any], spec: Mapping[str, Any], trade: dict[str, Any], moment: datetime) -> dict[str, Any]:
    sold, avg = _filled(trade, "SELL")
    mult = float(spec["mult"])
    proceeds = sold * avg * mult
    pnl = proceeds - float(trade.get("cost") or 0)
    trade["working"] = None
    trade["status"] = "closed"
    trade["exit"] = avg
    trade["exit_at"] = moment.isoformat()
    trade["exit_session"] = moment.date().isoformat()
    trade["pnl"] = pnl
    trade["exit_fill_source"] = FILL_SOURCE
    book["cash"] = float(book.get("cash") or 0) + proceeds
    return spec["alert"](
        "SELL",
        trade,
        f"{spec['head'](trade)} SELL {spec['text'](trade)} ×{sold} · Filled @ ${avg:.2f} (Webull paper) · "
        f"{trade.get('exit_reason')} · P&L {pnl:+.2f}",
        "filled",
    )


def advance(
    book: dict[str, Any],
    broker: Any,
    spec: Mapping[str, Any],
    *,
    quote_fn: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    now: datetime | None = None,
    entry_deadline: Callable[[Mapping[str, Any], datetime], bool] | None = None,
) -> list[dict[str, Any]]:
    """Refresh, walk, cancel and settle every working order on this book."""
    moment = _now_ct(now)
    alerts: list[dict[str, Any]] = []
    step = float(spec.get("step_sec") or 15)
    chase = float(spec.get("entry_chase_sec") or 90)
    for trade in list(book.get("trades") or []):
        if not isinstance(trade, dict) or trade.get("status") not in WORKING:
            continue
        work = trade.get("working")
        if not isinstance(work, Mapping) or not work.get("id"):
            continue
        intent = _intent(broker, str(work["id"]))
        if intent is None:
            logger.warning("isolated desk %s lost order %s", spec["desk"], work.get("id"))
            continue
        side = str(work.get("side"))
        if intent.status in {"submitted", "partial"}:
            broker.refresh_fill(intent)
        elif intent.status == "deferred" and (intent.meta or {}).get("retryable"):
            broker.submit(intent)
        _sync_row(trade, intent)
        qty_goal = int(trade.get("target_qty") or _qty(trade, spec)) if side == "BUY" else _qty(trade, spec)
        done, _avg = _filled(trade, side)
        if done >= qty_goal:
            alert = _finish_buy(book, spec, trade, moment) if side == "BUY" else _finish_sell(book, spec, trade, moment)
            alerts.append(alert)
            continue
        if intent.status in {"rejected", "cancelled", "skipped"}:
            if side == "BUY":
                alerts.append(_finish_buy(book, spec, trade, moment))
                continue
            # A sell that died (DAY order expired, reject) goes out again.
            trade["working"] = None
            quote = quote_fn(trade) or {}
            again = open_exit(
                book, broker, spec, trade, str(trade.get("exit_reason") or "exit"),
                bid=quote.get("bid"), ask=quote.get("ask"), mark=trade.get("last_mark"), now=moment,
            )
            if again:
                alerts.append(again)
            continue
        if intent.status not in {"submitted", "partial"}:
            continue  # deferred / staged: wait for the session
        if _age_sec(work.get("placed_at"), moment) < step:
            continue
        quote = quote_fn(trade) or {}
        limit = float(intent.limit_price or work.get("limit") or 0)
        if side == "BUY":
            past = entry_deadline(trade, moment) if entry_deadline else False
            if past or _age_sec(work.get("first_at"), moment) >= chase:
                broker.cancel(intent)
                if intent.status != "cancelled":
                    continue  # cancel not confirmed; try again next tick
                _confirm(broker, intent)
                _sync_row(trade, intent)
                done, _avg = _filled(trade, "BUY")
                if done < 1:
                    trade["not_filled_reason"] = (
                        f"still working after {int(chase)} s" if not past else "entry window closed"
                    )
                alerts.append(_finish_buy(book, spec, trade, moment))
                continue
            new_limit = next_buy_limit(spec, ask=quote.get("ask"), signal_ask=work.get("signal_px"), limit=limit)
        else:
            new_limit = next_sell_limit(spec, bid=quote.get("bid"), limit=limit)
        if new_limit is None:
            continue
        broker.cancel(intent)
        if intent.status != "cancelled":
            continue
        _confirm(broker, intent)
        _sync_row(trade, intent)
        done, _avg = _filled(trade, side)
        if done >= qty_goal:
            alert = _finish_buy(book, spec, trade, moment) if side == "BUY" else _finish_sell(book, spec, trade, moment)
            alerts.append(alert)
            continue
        _place(
            broker,
            spec,
            trade,
            side,
            qty_goal - done,
            new_limit,
            now=moment,
            signal_px=work.get("signal_px"),
            reason=f"reprice {side.lower()}",
        )
    return alerts


def pending_cost(book: Mapping[str, Any], spec: Mapping[str, Any]) -> float:
    """Cash a working entry would use at its current limit."""
    total = 0.0
    for trade in book.get("trades") or []:
        if not isinstance(trade, Mapping) or trade.get("status") != "pending":
            continue
        work = trade.get("working") or {}
        limit = _f(work.get("limit")) or _f(trade.get("signal_ask")) or 0.0
        total += limit * int(trade.get("target_qty") or 0) * float(spec["mult"])
    return total


# ---- isolation from the main AutoTrader --------------------------------------


def isolated_holdings(paths: Mapping[str, Path] | None = None) -> dict[str, float]:
    """Option contracts held on Webull by Million (by OCC symbol → contracts).

    Read from the Million book: routed trades that are open or closing, plus
    the filled part of a pending entry. The main AutoTrader subtracts these
    from the shared account's positions so it never manages or sells them.
    Kotegawa holds stocks, which the option position parser ignores.
    """
    import json

    targets = paths or {"million": ROOT / "outputs" / "million_challenge_state.json"}
    held: dict[str, float] = {}
    for _desk, path in targets.items():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for trade in raw.get("trades") or []:
            if not isinstance(trade, dict) or trade.get("route") != "webull":
                continue
            contract = str(trade.get("contract") or "")
            if not contract:
                continue
            status = trade.get("status")
            if status in {"open", "closing"}:
                qty = int(trade.get("contracts") or 0) - _filled(trade, "SELL")[0]
            elif status == "pending":
                qty = _filled(trade, "BUY")[0]
            else:
                continue
            if qty > 0:
                held[contract] = held.get(contract, 0.0) + float(qty)
    return held


def subtract_isolated(rows: list[dict[str, Any]], held: Mapping[str, float], account_id: Any) -> list[dict[str, Any]]:
    """Drop the isolated desks' contracts from one account's option positions."""
    if not held:
        return rows
    left = dict(held)
    out: list[dict[str, Any]] = []
    for pos in rows:
        contract = str(pos.get("contract") or "")
        acct = str(pos.get("account_id") or "")
        take = left.get(contract, 0.0) if (not account_id or acct == str(account_id)) else 0.0
        if take > 0:
            qty = float(pos.get("quantity") or 0)
            used = min(qty, take)
            left[contract] = take - used
            qty -= used
            if qty <= 1e-9:
                continue
            pos = {**pos, "quantity": qty, "isolated_subtracted": used}
        out.append(pos)
    return out


# ---- fast tick ----------------------------------------------------------------


def tick(config_path: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Advance working orders for both desks between portal cycles.

    Runs under the paper-desk cycle lock; a running cycle wins and this tick
    is skipped. Nothing here opens a new entry or decides an exit.
    """
    from odte_scanner.desks import service

    if not service._CYCLE_LOCK.acquire(blocking=False):
        return {"skipped": "cycle running"}
    try:
        from odte_scanner.config import load_config

        cfg = load_config(config_path)
        out: dict[str, Any] = {}
        from odte_scanner.desks.kotegawa import advance_webull as kot_advance
        from odte_scanner.desks.million import advance_webull as mil_advance

        for name, fn, section in (
            ("million", mil_advance, "million_challenge"),
            ("kotegawa", kot_advance, "kotegawa"),
        ):
            try:
                out[name] = fn(cfg.get(section) or {}, live_trading=cfg.get("live_trading") or {}, now=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("isolated desk tick %s failed: %s", name, exc)
                out[name] = {"error": str(exc)[:200]}
        return out
    finally:
        service._CYCLE_LOCK.release()


_tick_started = False
_tick_lock = threading.Lock()


def start_tick_worker(config_path: str | None = None, pause_sec: float = 8.0) -> bool:
    """Daemon thread: one tick every ``pause_sec`` during the option session."""
    import os
    import time

    global _tick_started
    if str(os.environ.get("ISOLATED_DESK_TICK", "1")).strip().lower() in {"0", "false", "off", "no"}:
        return False
    with _tick_lock:
        if _tick_started:
            return False
        _tick_started = True

    def _loop() -> None:
        from odte_scanner.signals.hold_rules import option_session_open

        while True:
            try:
                if option_session_open(datetime.now(timezone.utc)):
                    tick(config_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("isolated desk tick loop: %s", exc)
            time.sleep(pause_sec)

    threading.Thread(target=_loop, name="isolated-desk-orders", daemon=True).start()
    return True
